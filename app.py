import os
import time
import uuid
from typing import List, Optional

import lightning as L
import requests
from lightning.app import frontend
from lightning_api_access import APIAccessFrontend
from lightning_app.utilities.frontend import AppInfo

from whisperer import (
    LoadBalancer,
    Locust,
    WhisperServe,
)
from whisperer.CONST import ENABLE_TRACKERS
from whisperer.utility.trackers import trackers
import requests
import json
import streamlit as st

HEADERS = {'Accept': 'application/json', 'Content-Type': 'application/json'}


def transcribe_app(states):
    st.write('Transcribe a video')
    st.write(states.whisperer_url)
    url = st.text_input("URL of a video to transcribe", value="")
    if st.button('Transcribe'):
        with st.spinner('Transcribing...'):
            response = requests.post(f'{states.whisperer_url}/api/predict',
                                     headers=HEADERS,
                                     data=json.dumps({'video_url': url}))
            contents = json.loads(response._content)
            st.json(contents)


class TranscribeUI(L.LightningFlow):
    """The WhispererFlow is a LightningFlow component that handles all the servers and uses load balancer to spawn up and
    shutdown based on current requests in the queue.

    Args:
        initial_num_workers: Number of works to start when app initializes.
        autoscale_interval: Number of seconds to wait before checking whether to upscale or downscale the works.
        max_batch_size: Number of requests to process at once.
        batch_timeout_secs: Number of seconds to wait before sending the requests to process.
        gpu_type: GPU type to use for the works.
        max_workers: Max numbers of works to spawn to handle the incoming requests.
        autoscale_down_limit: Lower limit to determine when to stop works.
        autoscale_up_limit: Upper limit to determine when to spawn up a new work.
    """

    def __init__(
        self,
        initial_num_workers: int = 1,
        autoscale_interval: int = 1 * 30,
        max_batch_size: int = 4,
        batch_timeout_secs: int = 10,
        gpu_type: str = "gpu-fast",
        max_workers: int = 20,
        autoscale_down_limit: Optional[int] = None,
        autoscale_up_limit: Optional[int] = None,
        load_testing: Optional[bool] = False,
    ):
        super().__init__()
        self.hide_footer_shadow = True
        self.load_balancer_started = False
        self._initial_num_workers = initial_num_workers
        self._num_workers = 0
        self._work_registry = {}
        self.autoscale_interval = autoscale_interval
        self.max_workers = max_workers
        self.autoscale_down_limit = autoscale_down_limit or initial_num_workers
        self.autoscale_up_limit = autoscale_up_limit or initial_num_workers * max_batch_size
        self.load_testing = load_testing or os.getenv("MUSE_LOAD_TESTING", False)
        self.fake_trigger = 0
        self.gpu_type = gpu_type
        self._last_autoscale = time.time()

        self.load_balancer = LoadBalancer(max_batch_size=max_batch_size,
                                          batch_timeout_secs=batch_timeout_secs,
                                          cache_calls=True,
                                          parallel=True)
        for i in range(initial_num_workers):
            work = WhisperServe(cache_calls=True, parallel=True)
            self.add_work(work)

        if self.load_testing:
            self.locust = Locust(locustfile="./scripts/locustfile.py")

        self.whisperer_url = ""

    @property
    def model_servers(self) -> List[WhisperServe]:
        works = []
        for i in range(self._num_workers):
            work: WhisperServe = self.get_work(i)
            works.append(work)
        return works

    def add_work(self, work) -> str:
        work_attribute = uuid.uuid4().hex
        work_attribute = f"model_serve_{self._num_workers}_{str(work_attribute)}"
        setattr(self, work_attribute, work)
        self._work_registry[self._num_workers] = work_attribute
        self._num_workers += 1
        return work_attribute

    def remove_work(self, index: int) -> str:
        work_attribute = self._work_registry[index]
        del self._work_registry[index]
        work = getattr(self, work_attribute)
        work.stop()
        self._num_workers -= 1
        return work_attribute

    def get_work(self, index: int):
        work_attribute = self._work_registry[index]
        work = getattr(self, work_attribute)
        return work

    def run(self):  # noqa: C901
        if os.environ.get("TESTING_LAI"):
            print("⚡ Lightning Dream App! ⚡")

        # provision these works early
        if not self.load_balancer.is_running:
            self.load_balancer.run([])

        for model_serve in self.model_servers:
            model_serve.run()

        if any(model_serve.url for model_serve in self.model_servers) and not self.load_balancer_started:
            # run the load balancer when one of the model servers is ready
            self.load_balancer.run([serve.url for serve in self.model_servers if serve.url])
            self.load_balancer_started = True

        if self.load_balancer.url:  # hack for getting the work url
            self.whisperer_url = self.load_balancer.url

        if self.load_testing and self.load_balancer.url:
            self.locust.run(self.load_balancer.url)

        if self.load_balancer.url:
            self.fake_trigger += 1
            self.autoscale()

    def configure_layout(self):
        return frontend.StreamlitFrontend(render_fn=transcribe_app)

    # def configure_layout(self):
    #     ui = [{"name": "Muse App" if self.load_testing else None, "content": self.ui}]
    #     if self.load_testing:
    #         ui.append({"name": "Locust", "content": self.locust.url})

    #     return ui

    def autoscale(self):
        """Upscale and down scale model inference works based on the number of requests."""
        if time.time() - self._last_autoscale < self.autoscale_interval:
            return

        self.load_balancer.update_servers(self.model_servers)

        num_requests = int(requests.get(f"{self.load_balancer.url}/num-requests").json())
        num_workers = len(self.model_servers)

        # upscale
        if num_requests > self.autoscale_up_limit and num_workers < self.max_workers:
            idx = self._num_workers
            print(f"Upscale to {self._num_workers + 1}")
            work = WhisperServe(cache_calls=True, parallel=True)
            new_work_id = self.add_work(work)
            print("new work id:", new_work_id)

        # downscale
        elif num_requests < self.autoscale_down_limit and num_workers > self._initial_num_workers:
            idx = self._num_workers - 1
            print(f"Downscale to {idx}")
            print("prev num servers:", len(self.model_servers))
            removed_id = self.remove_work(idx)
            print("removed:", removed_id)
            print("new num servers:", len(self.model_servers))
            self.load_balancer.update_servers(self.model_servers)
        self._last_autoscale = time.time()


class WhispererFlow(L.LightningFlow):

    def __init__(self):
        super().__init__()
        self.ui = TranscribeUI()

    def run(self):
        self.ui.run()

    def configure_layout(self):
        tab1 = [{"name": "home", "content": self.ui}]
        return tab1


if __name__ == "__main__":
    app = L.LightningApp(
        WhispererFlow(),
        info=AppInfo(
            title="Use AI to inspire your art.",
            favicon="https://storage.googleapis.com/grid-static/muse/favicon.ico",
            description="Bring your words to life in seconds - powered by Lightning AI and Stable Diffusion.",
            image="https://storage.googleapis.com/grid-static/header.png",
            meta_tags=[
                '<meta name="theme-color" content="#792EE5" />',
                '<meta name="image" content="https://storage.googleapis.com/grid-static/header.png">'
                '<meta itemprop="name" content="Use AI to inspire your art.">'
                '<meta itemprop="description" content="Bring your words to life in seconds - powered by Lightning AI and Stable Diffusion.">'  # noqa
                '<meta itemprop="image" content="https://storage.googleapis.com/grid-static/header.png">'
                # <!-- Twitter -->
                '<meta name="twitter:card" content="summary">'
                '<meta name="twitter:title" content="Use AI to inspire your art.">'
                '<meta name="twitter:description" content="Bring your words to life in seconds - powered by Lightning AI and Stable Diffusion.">'  # noqa
                '<meta name="twitter:site" content="https://lightning.ai/muse">'
                '<meta name="twitter:domain" content="https://lightning.ai/muse">'
                '<meta name="twitter:creator" content="@LightningAI">'
                '<meta name="twitter:image:src" content="https://storage.googleapis.com/grid-static/header.png">'
                # <!-- Open Graph general (Facebook, Pinterest & Google+) -->
                '<meta name="og:title" content="Use AI to inspire your art.">'
                '<meta name="og:description" content="Bring your words to life in seconds - powered by Lightning AI and Stable Diffusion.">'  # noqa
                '<meta name="og:url" content="https://lightning.ai/muse">'
                '<meta property="og:image" content="https://storage.googleapis.com/grid-static/header.png" />',
                '<meta property="og:image:type" content="image/png" />',
                '<meta property="og:image:height" content="1114" />'
                '<meta property="og:image:width" content="1112" />',
                *(trackers if ENABLE_TRACKERS else []),
            ],
        ),
        root_path=os.getenv("MUSE_ROOT_PATH", ""),
    )
