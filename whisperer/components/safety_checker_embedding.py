import os
from typing import List, Optional

import lightning as L
import torch
from lightning import BuildConfig, LightningWork
from lightning.app.storage import Drive

from whisperer.CONST import NSFW_PROMPTS


class LightningFlashBuildConfig(BuildConfig):

    def build_commands(self) -> List[str]:
        url_flash = "https://github.com/rohitgr7/lightning-flash.git"
        return [f"pip install 'git+{url_flash}@rel/pl_18#egg=lightning-flash[text]'"]


class SafetyCheckerEmbedding(LightningWork):

    def __init__(self, nsfw_list: Optional[List] = None, drive: Optional[Drive] = None):
        super().__init__(parallel=False,
                         cloud_compute=L.CloudCompute("cpu-medium"),
                         cloud_build_config=LightningFlashBuildConfig())
        self.drive = drive
        self.safety_embeddings_filename = "safety_embedding.pt"

    def run(self):

        from flash import Trainer
        from flash.text import TextClassificationData, TextClassifier

        datamodule = TextClassificationData.from_lists(predict_data=NSFW_PROMPTS,
                                                       batch_size=4,
                                                       num_workers=os.cpu_count())

        model = TextClassifier(backbone="clip_vitb32", num_classes=2).cpu().float()
        torch.cuda.empty_cache()
        embedder = model.as_embedder("adapter.backbone")

        trainer = Trainer()
        embedding_batches = trainer.predict(embedder, datamodule)
        embeddings = torch.stack([embedding for embedding_batch in embedding_batches for embedding in embedding_batch],
                                 dim=0)

        torch.save(embeddings, self.safety_embeddings_filename)
        self.drive.put(self.safety_embeddings_filename)
