import argparse
import os
import inspect
from time import perf_counter
import numpy as np
# openvino
from openvino.runtime import Core
# tokenizer
from transformers import CLIPTokenizer
# scheduler
from diffusers import LMSDiscreteScheduler
# utils
from tqdm import tqdm
import cv2
from huggingface_hub import hf_hub_download
import requests


class StableDiffusion:
    def __init__(
            self,
            scheduler,
            model="bes-dev/stable-diffusion-v1-4-openvino",
            tokenizer="openai/clip-vit-large-patch14",
            device="CPU",
    ):
        self.tokenizer = CLIPTokenizer.from_pretrained(tokenizer)
        self.scheduler = scheduler
        # models
        self.core = Core()
        # text features
        self._text_encoder = self.core.read_model(
            hf_hub_download(repo_id=model, filename="text_encoder.xml"),
            hf_hub_download(repo_id=model, filename="text_encoder.bin")
        )
        self.text_encoder = self.core.compile_model(self._text_encoder, device)
        # diffusion
        self._unet = self.core.read_model(
            hf_hub_download(repo_id=model, filename="unet.xml"),
            hf_hub_download(repo_id=model, filename="unet.bin")
        )
        self.unet = self.core.compile_model(self._unet, device)
        self.latent_shape = tuple(self._unet.inputs[0].shape)[1:]
        # decoder
        self._vae = self.core.read_model(
            hf_hub_download(repo_id=model, filename="vae_decoder.xml"),
            hf_hub_download(repo_id=model, filename="vae_decoder.bin")
        )
        self.vae = self.core.compile_model(self._vae, device)

    def __call__(self, prompt, num_inference_steps=32, guidance_scale=7.5, eta=0.0, server="",
                 token="",
                 next_prompt={"id": 0}):
        def result(var): return next(iter(var.values()))

        # extract condition
        tokens = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True
        ).input_ids
        text_embeddings = result(
            self.text_encoder.infer_new_request({"tokens": np.array([tokens])})
        )

        # do classifier free guidance
        if guidance_scale > 1.0:
            tokens_uncond = self.tokenizer(
                "",
                padding="max_length",
                max_length=self.tokenizer.model_max_length,
                truncation=True
            ).input_ids
            uncond_embeddings = result(
                self.text_encoder.infer_new_request(
                    {"tokens": np.array([tokens_uncond])})
            )
            text_embeddings = np.concatenate(
                (uncond_embeddings, text_embeddings), axis=0)

        # make noise
        latents = np.random.randn(*self.latent_shape)

        # set timesteps
        accepts_offset = "offset" in set(inspect.signature(
            self.scheduler.set_timesteps).parameters.keys())
        extra_set_kwargs = {}
        if accepts_offset:
            extra_set_kwargs["offset"] = 1

        self.scheduler.set_timesteps(num_inference_steps, **extra_set_kwargs)

        # if we use LMSDiscreteScheduler, let's make sure latents are mulitplied by sigmas
        if isinstance(self.scheduler, LMSDiscreteScheduler):
            latents = latents * self.scheduler.sigmas[0]

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        accepts_eta = "eta" in set(inspect.signature(
            self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        start_t = perf_counter()

        for i, t in enumerate(self.scheduler.timesteps):
            # expand the latents if we are doing classifier free guidance
            latent_model_input = np.stack(
                [latents, latents], 0) if guidance_scale > 1.0 else latents
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                sigma = self.scheduler.sigmas[i]
                latent_model_input = latent_model_input / \
                    ((sigma**2 + 1) ** 0.5)

            print("Starting iteration", i)

            # predict the noise residual
            noise_pred = result(self.unet.infer_new_request({
                "latent_model_input": latent_model_input,
                "t": t,
                "encoder_hidden_states": text_embeddings
            }))

            end_t = perf_counter()

            total = end_t - start_t
            per_iteration = total / (i + 1)
            estimated_total = per_iteration * num_inference_steps
            percent = (i / num_inference_steps) * 100

            print(
                f"End of iteration {i}, total time: {round(total)}s, {round(per_iteration)}s/it. Time till done {round(estimated_total - total)}s ({round(percent)}% done)"
            )
            requests.put(
                f"{server}/prompt/{next_prompt['id']}?token={token}", json={"being_generated": True, "generated_percent": round(percent)})

            # perform guidance
            if guidance_scale > 1.0:
                noise_pred = noise_pred[0] + guidance_scale * \
                    (noise_pred[1] - noise_pred[0])

            # compute the previous noisy sample x_t -> x_t-1
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                latents = self.scheduler.step(
                    noise_pred, i, latents, **extra_step_kwargs)["prev_sample"]
            else:
                latents = self.scheduler.step(
                    noise_pred, t, latents, **extra_step_kwargs)["prev_sample"]

        image = result(self.vae.infer_new_request({
            "latents": np.expand_dims(latents, 0)
        }))

        # convert tensor to opencv's image format
        image = (image / 2 + 0.5).clip(0, 1)
        image = (image[0].transpose(1, 2, 0)[
                 :, :, ::-1] * 255).astype(np.uint8)
        return image


def main(args):
    if args.seed is not None:
        np.random.seed(args.seed)
    scheduler = LMSDiscreteScheduler(
        beta_start=args.beta_start,
        beta_end=args.beta_end,
        beta_schedule=args.beta_schedule,
        tensor_format="np"
    )
    stable_diffusion = StableDiffusion(
        model=args.model,
        scheduler=scheduler,
        tokenizer=args.tokenizer
    )
    image = stable_diffusion(
        prompt=args.prompt,
        num_inference_steps=args.num_inference_steps,
        guidance_scale=args.guidance_scale,
        eta=args.eta
    )
    cv2.imwrite(args.output, image)


def run_stable_diffusion(prompt, iterations, seed, output, server, token, next_prompt):
    np.random.seed(seed)
    scheduler = LMSDiscreteScheduler(
        beta_start=0.00085,
        beta_end=0.012,
        beta_schedule="scaled_linear",
        tensor_format="np"
    )
    stable_diffusion = StableDiffusion(
        model="bes-dev/stable-diffusion-v1-4-openvino",
        scheduler=scheduler,
        tokenizer="openai/clip-vit-large-patch14"
    )
    image = stable_diffusion(
        prompt=prompt,
        num_inference_steps=iterations,
        guidance_scale=7.5,
        eta=0.0,
        server=server,
        token=token,
        next_prompt=next_prompt
    )
    cv2.imwrite(output, image)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # pipeline configure
    parser.add_argument(
        "--model", type=str, default="bes-dev/stable-diffusion-v1-4-openvino", help="model name")
    # scheduler params
    parser.add_argument("--beta-start", type=float,
                        default=0.00085, help="LMSDiscreteScheduler::beta_start")
    parser.add_argument("--beta-end", type=float, default=0.012,
                        help="LMSDiscreteScheduler::beta_end")
    parser.add_argument("--beta-schedule", type=str, default="scaled_linear",
                        help="LMSDiscreteScheduler::beta_schedule")
    # diffusion params
    parser.add_argument("--num-inference-steps", type=int,
                        default=32, help="num inference steps")
    parser.add_argument("--guidance-scale", type=float,
                        default=7.5, help="guidance scale")
    parser.add_argument("--eta", type=float, default=0.0, help="eta")
    # tokenizer
    parser.add_argument("--tokenizer", type=str,
                        default="openai/clip-vit-large-patch14", help="tokenizer")
    # prompt
    parser.add_argument(
        "--prompt", type=str, default="Street-art painting of Emilia Clarke in style of Banksy, photorealism", help="prompt")
    # seed
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for generating consistent images per prompt")
    # output name
    parser.add_argument("--output", type=str,
                        default="output.png", help="output image name")
    args = parser.parse_args()
    main(args)
