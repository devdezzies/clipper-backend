import json
import pathlib
import shutil
import subprocess
import time
import uuid
import modal 
from pydantic import BaseModel
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import os
from supabase import create_client
import asyncio
import whisperx
from google import genai

class ProcessVideoRequest(BaseModel): 
    video_path: str

# setup environment on the server (modal)
image = (modal.Image.from_registry(
    "nvidia/cuda:12.4.0-devel-ubuntu22.04", add_python="3.12")
    .apt_install(["ffmpeg", "libgl1-mesa-glx", "wget", "libcudnn8", "libcudnn8-dev"])
    .pip_install_from_requirements("requirements.txt")
    .run_commands(["mkdir -p /usr/share/fonts/truetype/custom", 
                   "wget -O /usr/share/fonts/truetype/custom/Anton-Regular.ttf https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf", 
                   "fc-cache -f -v"])
    .add_local_dir("asd", "/asd", copy=True))

# initiate instance
app = modal.App("podcast-clipper", image=image)

# place for storing models (saving between different runs)
volume = modal.Volume.from_name(
    "podcast-clipper-model-cache", create_if_missing=True
)

mount_path = "/root/.cache/torch"

auth_scheme = HTTPBearer()

@app.cls(gpu="L40S", timeout=900, retries=0, scaledown_window=20, secrets=[modal.Secret.from_name("podcast-clipper-secret")], volumes={mount_path: volume})
class PodcastClipper: 
    @modal.enter()
    async def load_model(self): 
        # setup supabase client
        print("Creating supabase client")
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        self.supabase = create_client(url, key)
        self.bucket_name = os.environ["BUCKET_NAME"]

        # load whisper model 
        print("Loading whisper model")
        self.whisperx_model = whisperx.load_model("large-v2", device="cuda", compute_type="float16")

        self.alignment_model, self.metadata = whisperx.load_align_model(
            language_code="en", 
            device="cuda"
        )

        print("Transcription model loaded")

        # gemini client
        print("Creating gemini client...")
        self.gemini_client = genai.Client(api_key=os.environ["GEMINI_SECRET"])
        print("Created gemini client")

    def transcribe_video(self, base_dir: str, video_path: str) -> str: 
        audio_path = base_dir / "audio.wav"
        extract_cmd = f"ffmpeg -i {video_path} -vn -acodec pcm_s16le -ar 16000 -ac 1 {audio_path}"
        subprocess.run(extract_cmd, shell=True, check=True, capture_output=True)

        print("Starting transcription with WhisperX...")

        start_time = time.time()

        audio = whisperx.load_audio(str(audio_path))
        result = self.whisperx_model.transcribe(audio, batch_size=16)

        result = whisperx.align(
            result["segments"], 
            self.alignment_model, 
            self.metadata, 
            audio, 
            device="cuda", 
            return_char_alignments=False
        )

        duration = time.time() - start_time
        print("Transcription and alignment took " + str(duration) + " seconds")

        segments = []

        if "segments" in result: 
            for word_segment in result["segments"]: 
                segments.append({
                    "start": word_segment["start"], 
                    "end": word_segment["end"], 
                    "word": word_segment["text"]
                })

        return json.dumps(segments)
    
    def identify_moments(self, transcript: dict): 
        response = self.gemini_client.models.generate_content(
            model="gemini-2.5-flash", 
            contents="""
     This is a podcast video transcript consisting of word, along with each words's start and end time. I am looking to create clips between a minimum of 30 and maximum of 60 seconds long. The clip should never exceed 60 seconds.

    Your task is to find and extract stories, or question and their corresponding answers from the transcript.
    Each clip should begin with the question and conclude with the answer.
    It is acceptable for the clip to include a few additional sentences before a question if it aids in contextualizing the question.

    Please adhere to the following rules:
    - Ensure that clips do not overlap with one another.
    - Start and end timestamps of the clips should align perfectly with the sentence boundaries in the transcript.
    - Only use the start and end timestamps provided in the input. modifying timestamps is not allowed.
    - Format the output as a list of JSON objects, each representing a clip with 'start' and 'end' timestamps: [{"start": seconds, "end": seconds}, ...clip2, clip3]. The output should always be readable by the python json.loads function.
    - Aim to generate longer clips between 40-60 seconds, and ensure to include as much content from the context as viable.

    Avoid including:
    - Moments of greeting, thanking, or saying goodbye.
    - Non-question and answer interactions.

    If there are no valid clips to extract, the output should be an empty list [], in JSON format. Also readable by json.loads() in Python.

    The transcript is as follows:\n\n""" + str(transcript))
        
        print(f"Identified moments response: {response.text}")
        return response.text
    
    def process_clip(self, base_dir: str, original_video_path: str, start_time: float, end_time: float, clip_index: int, transcript_segments: list):
        pass

    @modal.fastapi_endpoint(method="POST")
    async def process_video(self, request: ProcessVideoRequest, token: HTTPAuthorizationCredentials = Depends(auth_scheme)):
        print("processing videos " + request.video_path)

        if token.credentials != os.environ["AUTH_TOKEN"]:
            raise HTTPException(status_code=401, detail="Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

        run_id = str(uuid.uuid4())
        base_dir = pathlib.Path("/tmp") / run_id
        base_dir.mkdir(parents=True, exist_ok=True)

        # download video file
        video_path = base_dir / "input.mp4"
        with open(video_path, "wb+") as f: 
            response = await asyncio.to_thread(
                self.supabase.storage.from_(self.bucket_name).download,
                request.video_path,
            )
            f.write(response)

        # transcribe the video
        transcript_segments_json = self.transcribe_video(base_dir, video_path)
        transcript_segments = json.loads(transcript_segments_json)

        # identify moments for clips
        print("Identifying moments...")
        identified_moments_raw = self.identify_moments(transcript_segments)

        cleaned_json_string = identified_moments_raw.strip()
        if cleaned_json_string.startswith("```json"): 
            cleaned_json_string = cleaned_json_string[len("```json"):].strip()
        if cleaned_json_string.endswith("```"): 
            cleaned_json_string = cleaned_json_string[:-len("```")].strip()

        clip_moments = json.loads(cleaned_json_string)
        if not clip_moments or not isinstance(clip_moments, list): 
            print("Error identifying moments as a list")
            clip_moments = []

        print(clip_moments)

        # processing clip moments
        # for index, moment in enumerate(clip_moments[:3]): 
        #     if "start" in moment and "end" in moment: 
        #         print("Processing clip " + str(index) + " from " + str(moment["start"]) + " to " + str(moment["end"]))

        
        # # cleaning up 
        # if base_dir.exists(): 
        #     print("Cleaning up temp directory after " + base_dir)
        #     shutil.rmtree(base_dir, ignore_errors=True)

# entrypoint        
@app.local_entrypoint()
def main(): 
    import requests 

    podcast_clipper = PodcastClipper()
    url = podcast_clipper.process_video.web_url

    payload = {
        "video_path": "test1/input.mp4"
    }

    headers = {
        "Content-Type": "application/json", 
        "Authorization": "Bearer 123123"
    }

    response = requests.post(url, json=payload, headers=headers)

    response.raise_for_status()
    result = response.json()
    print(result)

if __name__ == "__main__":
    main()