import os
import re
import time
import mmap
import datetime
import aiohttp
import aiofiles
import asyncio
import logging
import requests
import tgcrypto
import subprocess
import concurrent.futures
from math import ceil
from utils import progress_bar
from pyrogram import Client, filters
from pyrogram.types import Message
from io import BytesIO
from pathlib import Path  
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from base64 import b64decode
import math
import m3u8
from urllib.parse import urljoin
from vars import *  # Your config vars
from db import Database

db = Database()

# ---------------- DRM TOKEN ---------------- #
drm_token = None  # Global DRM token variable

def set_drm_token(token):
    global drm_token
    drm_token = token
    print(f"[INFO] DRM Token set: {drm_token}")

# ---------------- Tools ---------------- #
TOOLS_DIR = os.path.join(os.path.dirname(__file__), "tools")
MP4DECRYPT = os.path.join(TOOLS_DIR, "mp4decrypt")  # mp4decrypt path

# ---------------- Helper Functions ---------------- #
def duration(filename):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                             "format=duration", "-of",
                             "default=noprint_wrappers=1:nokey=1", filename],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    return float(result.stdout)

def get_mps_and_keys(api_url):
    response = requests.get(api_url)
    response_json = response.json()
    mpd = response_json.get('mpd_url')
    keys = response_json.get('keys')
    return mpd, keys

def exec(cmd):
    process = subprocess.run(cmd, stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    output = process.stdout.decode()
    print(output)
    return output

def pull_run(work, cmds):
    with concurrent.futures.ThreadPoolExecutor(max_workers=work) as executor:
        print("Waiting for tasks to complete")
        fut = executor.map(exec,cmds)

# ---------------- Async Download Functions ---------------- #
async def aio(url,name):
    headers = {"Authorization": f"Bearer {drm_token}"} if drm_token else {}
    k = f'{name}.pdf'
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                f = await aiofiles.open(k, mode='wb')
                await f.write(await resp.read())
                await f.close()
    return k

async def download(url,name):
    headers = {"Authorization": f"Bearer {drm_token}"} if drm_token else {}
    ka = f'{name}.pdf'
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                f = await aiofiles.open(ka, mode='wb')
                await f.write(await resp.read())
                await f.close()
    return ka

async def pdf_download(url, file_name, chunk_size=1024 * 10):
    headers = {"Authorization": f"Bearer {drm_token}"} if drm_token else {}
    if os.path.exists(file_name):
        os.remove(file_name)
    r = requests.get(url, allow_redirects=True, stream=True, headers=headers)
    with open(file_name, 'wb') as fd:
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                fd.write(chunk)
    return file_name

# ---------------- Video Download / Decrypt ---------------- #
async def decrypt_and_merge_video(mpd_url, keys_string, output_path, output_name, quality="720"):
    try:
        output_path = Path(output_path)
        output_path.mkdir(parents=True, exist_ok=True)

        if not os.path.exists(MP4DECRYPT):
            raise FileNotFoundError(f"mp4decrypt tool not found at {MP4DECRYPT}")
        try:
            os.chmod(MP4DECRYPT, 0o755)
        except Exception as e:
            print(f"Warning: Could not set permissions for mp4decrypt: {str(e)}")

        # DRM token header for yt-dlp
        cmd1 = f'yt-dlp -f "bv[height<={quality}]+ba/b" -o "{output_path}/file.%(ext)s" --allow-unplayable-format --no-check-certificate --external-downloader aria2c "{mpd_url}"'
        if drm_token:
            cmd1 += f' --add-header "Authorization: Bearer {drm_token}"'
        os.system(cmd1)

        avDir = list(output_path.iterdir())
        print("Decrypting")
        video_decrypted = False
        audio_decrypted = False

        for data in avDir:
            if data.suffix == ".mp4" and not video_decrypted:
                cmd2 = f'"{MP4DECRYPT}" {keys_string} --show-progress "{data}" "{output_path}/video.mp4"'
                result = subprocess.run(cmd2, shell=True, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"Error running mp4decrypt: {result.stderr}")
                    raise Exception(f"mp4decrypt failed: {result.stderr}")
                if (output_path / "video.mp4").exists():
                    video_decrypted = True
                data.unlink()
            elif data.suffix == ".m4a" and not audio_decrypted:
                cmd3 = f'"{MP4DECRYPT}" {keys_string} --show-progress "{data}" "{output_path}/audio.m4a"'
                result = subprocess.run(cmd3, shell=True, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"Error running mp4decrypt: {result.stderr}")
                    raise Exception(f"mp4decrypt failed: {result.stderr}")
                if (output_path / "audio.m4a").exists():
                    audio_decrypted = True
                data.unlink()

        if not video_decrypted or not audio_decrypted:
            raise FileNotFoundError("Decryption failed: video or audio file not found.")

        cmd4 = f'ffmpeg -i "{output_path}/video.mp4" -i "{output_path}/audio.m4a" -c copy "{output_path}/{output_name}.mp4"'
        os.system(cmd4)
        if (output_path / "video.mp4").exists():
            (output_path / "video.mp4").unlink()
        if (output_path / "audio.m4a").exists():
            (output_path / "audio.m4a").unlink()
        
        filename = output_path / f"{output_name}.mp4"
        if not filename.exists():
            raise FileNotFoundError("Merged video file not found.")

        return str(filename)

    except Exception as e:
        print(f"Error during decryption and merging: {str(e)}")
        raise

# ---------------- Fast Direct Download ---------------- #
async def fast_download(url, name):
    max_retries = 5
    retry_count = 0
    success = False
    headers = {"Authorization": f"Bearer {drm_token}"} if drm_token else {}

    while not success and retry_count < max_retries:
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        output_file = f"{name}.mp4"
                        with open(output_file, 'wb') as f:
                            while True:
                                chunk = await response.content.read(1024*1024)
                                if not chunk:
                                    break
                                f.write(chunk)
                        success = True
                        return [output_file]
        except Exception as e:
            print(f"\nError attempt {retry_count + 1}: {str(e)}")
            retry_count += 1
            await asyncio.sleep(3)
    return None

# ---------------- Telegram Bot Command Example ---------------- #
from pyrogram import Client, filters

@Client.on_message(filters.command("settoken") & filters.user(OWNER_ID))
async def set_token_handler(client, message):
    try:
        token = message.text.split(" ", 1)[1]
        set_drm_token(token)
        await message.reply_text(f"DRM Token set successfully!")
    except IndexError:
        await message.reply_text("Usage: /settoken <your_token>")
