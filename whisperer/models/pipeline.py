import warnings
import torch
import whisper
from pytube import YouTube


class WhisperModel:

    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        model = whisper.load_model('base', device=self.device)
        self.model = model
        self.decode_options = whisper.DecodingOptions()
        self.dtype = torch.float16
        fp16 = True
        if model.device == torch.device("cpu"):
            if torch.cuda.is_available():
                warnings.warn("Performing inference on CPU when CUDA is available")
            if self.dtype == torch.float16:
                warnings.warn("FP16 is not supported on CPU; using FP32 instead")
                self.dtype = torch.float32
                fp16 = False
        self.decode_options = whisper.DecodingOptions(fp16=fp16)
        torch.cuda.empty_cache()

    def __call__(self, batch):
        batch_size = len(batch)
        results = []
        with torch.inference_mode():
            print(f"Video processing started ({batch})")
            for sample in batch:
                video_url = sample.video_url
                yt = YouTube(video_url)

                streams = yt.streams.filter(only_audio=True)
                stream_url = streams[0].url
                length = yt.length
                if length >= 300:
                    raise ValueError("Please find a YouTube video shorter than 5 minutes."
                                     " Sorry about this, the server capacity is limited"
                                     " for the time being.")
                audio = whisper.load_audio(stream_url)
                result = self.model.transcribe(audio)
                results.append({video_url: result})
                print('pipe', results)
        return results
