import base64
import os.path
import tarfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import lightning as L
import numpy as np
import torch
from lightning.app.storage import Drive
from PIL import Image
from torch import autocast

from whisperer.CONST import IMAGE_SIZE, INFERENCE_REQUEST_TIMEOUT, KEEP_ALIVE_TIMEOUT
from whisperer.models import StableDiffusionModel
from whisperer.utility.data_io import Data, DataBatch, TimeoutException


def cos_sim(x, y):
    x = torch.nn.functional.normalize(x, p=2, dim=1)
    y = torch.nn.functional.normalize(y, p=2, dim=1)
    return torch.mm(x, y.transpose(0, 1))


class SafetyChecker:

    def __init__(self, embeddings_path):
        import clip as openai_clip

        self.model, self.preprocess = openai_clip.load("ViT-B/32", device="cpu")
        self.text_embeddings = torch.load(embeddings_path)

    def __call__(self, images):
        images = torch.stack([self.preprocess(img) for img in images])
        encoded_images = self.model.encode_image(images)

        similarity = cos_sim(encoded_images, self.text_embeddings)
        return torch.any(similarity > 0.24, dim=1).tolist()


@dataclass
class DiffusionBuildConfig(L.BuildConfig):
    requirements = ["fastapi==0.78.0", "uvicorn==0.17.6"]

    def build_commands(self):
        return [
            "git clone -b rel/pl_18 https://github.com/rohitgr7/stable-diffusion",
            "pip install -r stable-diffusion/requirements.txt",
            "pip install -e stable-diffusion",
            "pip install git+https://github.com/openai/CLIP.git",
        ]


class StableDiffusionServe(L.LightningWork):
    """The StableDiffusionServer handles the prediction.

    It initializes a model and expose an API to handle incoming requests and generate predictions.
    """

    def __init__(self,
                 safety_embeddings_drive: Optional[Drive] = None,
                 safety_embeddings_filename: str = None,
                 **kwargs):
        super().__init__(cloud_build_config=DiffusionBuildConfig(), **kwargs)
        self.safety_embeddings_drive = safety_embeddings_drive
        self.safety_embeddings_filename = safety_embeddings_filename
        self._model = None

    @staticmethod
    def download_weights(url: str, target_folder: Path):
        dest = target_folder / f"{os.path.basename(url)}"
        if not os.path.exists(dest):
            print("Downloading weights...")
            urllib.request.urlretrieve(url, dest)
            file = tarfile.open(dest)

            # extracting file
            file.extractall(target_folder)

    def build_model(self):
        """The `build_model(...)` method returns a model and the returned model is set to `self._model` state."""
        self.safety_embeddings_drive.get(self.safety_embeddings_filename)
        self._safety_checker = SafetyChecker(self.safety_embeddings_filename)

        print("loading model...")
        if torch.cuda.is_available():
            weights_folder = Path("resources/stable_diffusion_weights")
            weights_folder.mkdir(parents=True, exist_ok=True)

            self.download_weights("https://pl-public-data.s3.amazonaws.com/dream_stable_diffusion/sd_weights.tar.gz",
                                  weights_folder)

            self._model = StableDiffusionModel(weights_folder / "sd_weights")
            print("model loaded")
        else:
            self._model = None
            print("model set to None")

    @torch.inference_mode()
    def predict(self, dreams: List[Data], entry_time: int):
        if time.time() - entry_time > INFERENCE_REQUEST_TIMEOUT:
            raise TimeoutException()

        height = width = IMAGE_SIZE
        num_inference_steps = 50 if dreams[0].high_quality else 25

        prompts = [dream.prompt for dream in dreams]
        if torch.cuda.is_available():
            with autocast("cuda"):
                torch.cuda.empty_cache()
                pil_results = self._model(
                    prompts,
                    height=height,
                    width=width,
                    num_inference_steps=num_inference_steps,
                )
            nsfw_content = self._safety_checker(pil_results)
            for i, nsfw in enumerate(nsfw_content):
                if nsfw:
                    pil_results[i] = Image.open("assets/nsfw-warning.png")
        else:
            time.sleep(3)
            pil_results = [Image.fromarray(np.random.randint(0, 255, (height, width, 3), dtype="uint8"))] * len(prompts)

        results = []
        for image in pil_results:
            buffered = BytesIO()
            image.save(buffered, format="PNG")
            img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
            # make sure pil_results is a single item array or it'll rewrite image
            results.append({"image": f"data:image/png;base64,{img_str}"})

        return results

    def run(self):

        if False and self.safety_embeddings_filename not in self.safety_embeddings_drive.list("."):
            return

        import subprocess

        import uvicorn
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware

        if torch.cuda.is_available():
            subprocess.run("nvidia-smi", shell=True)

        if self._model is None:
            self.build_model()

        self._fastapi_app = app = FastAPI()
        app.POOL: ThreadPoolExecutor = None

        @app.on_event("startup")
        def startup_event():
            app.POOL = ThreadPoolExecutor(max_workers=1)

        @app.on_event("shutdown")
        def shutdown_event():
            app.POOL.shutdown(wait=False)

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/api/health")
        def health():
            return True

        @app.post("/api/predict")
        def predict_api(data: DataBatch):
            """Dream a muse. Defines the REST API which takes the text prompt, number of images and image size in the
            request body.

            This API returns an image generated by the model in base64 format.
            """
            try:
                entry_time = time.time()
                print(f"batch size: {len(data.batch)}")
                result = app.POOL.submit(
                    self.predict,
                    data.batch,
                    entry_time=entry_time,
                ).result(timeout=INFERENCE_REQUEST_TIMEOUT)
                return result
            except (TimeoutError, TimeoutException):
                # hack: once there is a timeout then all requests after that is getting timeout
                # old_pool = app.POOL
                # app.POOL = ThreadPoolExecutor(max_workers=1)
                # old_pool.shutdown(wait=False)
                # signal.signal(signal.SIGINT, lambda sig, frame: exit_threads(old_pool))
                raise TimeoutException()

        uvicorn.run(app,
                    host=self.host,
                    port=self.port,
                    timeout_keep_alive=KEEP_ALIVE_TIMEOUT,
                    access_log=False,
                    loop="uvloop")
