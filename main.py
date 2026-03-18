"""
ContextCat Media Generation Service
Cloud Run service that bridges GitLab Duo Flow with Google Vertex AI

Flow:
1. GitLab Webhook triggers this service when Issue is updated
2. Service reads storyboard JSON from Issue comment
3. Calls Imagen 4 to generate reference images
4. Calls Veo 3 to generate video clips with audio
5. Writes results back to GitLab Issue

Author: Chloe Kao x Claude (Anthropic)
License: MIT
Version: 1.1.0 - Fix: storyboard comments no longer trigger pipeline
"""

import os
import json
import re
import time
import logging
import threading
import requests
from flask import Flask, request, jsonify
import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Config from environment variables
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN")
GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.com")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


# Google Auth helper
def get_google_token():
    """Get Google access token for Vertex AI API calls."""
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)
    return credentials.token


# ===============================================================
# GITLAB HELPERS
# ===============================================================

def get_issue(project_id: int, issue_iid: int) -> dict:
    """Read a GitLab Issue and all its comments."""
    headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
    issue_url = f"{GITLAB_URL}/api/v4/projects/{project_id}/issues/{issue_iid}"
    issue_resp = requests.get(issue_url, headers=headers, timeout=30)
    issue_resp.raise_for_status()
    issue = issue_resp.json()
    notes_url = f"{GITLAB_URL}/api/v4/projects/{project_id}/issues/{issue_iid}/notes"
    notes_resp = requests.get(notes_url, headers=headers, params={"per_page": 100}, timeout=30)
    notes_resp.raise_for_status()
    issue["notes"] = notes_resp.json()
    return issue


def post_issue_comment(project_id: int, issue_iid: int, body: str):
    """Post a comment to a GitLab Issue."""
    headers = {"PRIVATE-TOKEN": GITLAB_TOKEN, "Content-Type": "application/json"}
    url = f"{GITLAB_URL}/api/v4/projects/{project_id}/issues/{issue_iid}/notes"
    resp = requests.post(url, headers=headers, json={"body": body}, timeout=30)
    resp.raise_for_status()
    logger.info(f"Posted comment to Issue #{issue_iid}")
    return resp.json()


def extract_storyboard_json(issue: dict) -> dict | None:
    """
    Find the most recent storyboard JSON block in Issue comments.
    Cat-2 posts its output in this format.
    """
    notes = sorted(issue.get("notes", []), key=lambda n: n["created_at"], reverse=True)
    for note in notes:
        body = note.get("body", "")
        match = re.search(r"```storyboard\s*\n(.*?)\n```", body, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse storyboard JSON: {e}")
                continue
    # Also check issue body
    body = issue.get("description", "")
    match = re.search(r"```storyboard\s*\n(.*?)\n```", body, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


# ===============================================================
# STORY BIBLE BUILDER
# ===============================================================

# Fixed character base — hardcoded to prevent AI hallucination across clips
# Natural cat colors constraint (prevent AI hallucination of unnatural colors)
CATS_CONSTRAINT = (
    "cats in natural realistic colors only — orange tabby, black, white, grey, "
    "calico, brown and cream — no unnatural colors like blue or green"
)

# Anti-hallucination: prevent unwanted screens/laptops appearing in non-laptop scenes
NO_SCREEN_CONSTRAINT = (
    "No laptops, computers, or screens in this scene unless explicitly described."
)

# Compositional constraint for laptop/screen scenes (曦 formula)
LAPTOP_CONSTRAINT = (
    "Front three-quarter view from the front-left side of the desk. "
    "The laptop screen is facing the camera, and the screen content is clearly visible. "
    "Both her face and the laptop screen are visible in the same frame. "
    "Not an over-the-shoulder shot. Not showing the back of the screen."
)

FIXED_CHARACTER = (
    "Asian woman, early 20s, black straight long hair, minimal white shirt, "
    "smooth skin, flawless face, perfectly consistent facial features"
)

# Anti-aging + quality suffix — appended to every visual prompt
QUALITY_SUFFIX = "early 20s, smooth skin, cinematic lighting, 8k resolution, perfectly consistent facial features"


def build_story_bible(clips: list, project_context: str = "") -> dict:
    """
    Build a Story Bible by calling Gemini 2.5 Pro.
    Gemini reads ALL visual blocks, understands the full story arc,
    and outputs:
      - story_bible: unified description prefix for Imagen 4
      - character_tags: fixed character tags for Veo 3 injection

    Fixed character base is hardcoded to prevent hallucination.
    """
    token = get_google_token()

    all_visuals = []
    for i, clip in enumerate(clips):
        clip_id = clip.get("clip_id", i + 1)
        all_visuals.append(f"Clip {clip_id}: {clip.get('visual', '')}")
    visuals_text = "\n".join(all_visuals)

    prompt = f"""You are a top cinematographer building a Story Bible for a 32-second video (4 clips x 8 seconds).
All 4 clips must look like ONE continuous story with the SAME character, SAME visual world.

FIXED CHARACTER (do not change): {FIXED_CHARACTER}

Here are the 4 visual descriptions:
{visuals_text}

Project context: {project_context[:300] if project_context else "Cinematic, warm, inspiring"}

Output a JSON object with exactly two fields:
1. "story_bible": A single paragraph (max 120 words) to prepend to every Imagen 4 prompt.
   Must include: character appearance, color palette, lighting style, mood arc, consistency instruction.
2. "character_tags": A short comma-separated tag string (max 30 words) to inject at the START of every Veo 3 prompt.
   Must include fixed character features + anti-aging tags.

Output ONLY valid JSON. No explanation, no markdown, no code blocks."""

    # Gemini 3.1 Pro only available on global endpoint, not us-central1
    endpoint = (
        f"https://global-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/global/"
        f"publishers/google/models/gemini-3.1-pro-preview:generateContent"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 500
        }
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        raw = result["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Strip markdown code blocks if present
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        story_bible = parsed.get("story_bible", "")
        character_tags = parsed.get("character_tags", FIXED_CHARACTER)
        logger.info(f"Gemini Story Bible: {story_bible[:120]}...")
        logger.info(f"Character tags: {character_tags[:80]}...")
        return {"story_bible": story_bible, "character_tags": character_tags}
    except Exception as e:
        logger.error(f"Gemini Story Bible failed: {e}, using fallback")
        fallback_bible = (
            f"{FIXED_CHARACTER}. Warm indoor lighting, cream and orange palette. "
            "Cinematic depth of field. Same character, same world across all frames."
        )
        return {"story_bible": fallback_bible, "character_tags": FIXED_CHARACTER}


# ===============================================================
# IMAGEN 4: REFERENCE IMAGE GENERATION
# ===============================================================

def generate_reference_image(visual_prompt: str, story_bible: str, clip_id: int) -> str | None:
    """
    Generate a reference image using Imagen 4 via Vertex AI.
    Returns the public HTTPS URL of the generated image in GCS.

    The story_bible is prepended to ensure visual consistency
    across all clips in the same video.
    """
    token = get_google_token()

    # Combine story bible with clip-specific visual prompt
    full_prompt = f"{story_bible} | {visual_prompt} | shot on 35mm lens, cinematic lighting, 8k resolution, {QUALITY_SUFFIX}"

    # Remove audio-related words that might confuse Imagen 4
    audio_words = ["voiceover", "narration", "says:", "music", "sound", "sfx", "audio"]
    for word in audio_words:
        full_prompt = re.sub(rf'\b{word}\b.*?[,.]', '', full_prompt, flags=re.IGNORECASE)

    logger.info(f"Imagen 4 prompt for clip {clip_id}: {full_prompt[:120]}...")

    endpoint = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/imagen-4.0-generate-001:predict"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    payload = {
        "instances": [{"prompt": full_prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": "16:9",
            "safetyFilterLevel": "block_some",
            "personGeneration": "allow_adult"
        }
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        result = resp.json()

        predictions = result.get("predictions", [])
        if predictions and "bytesBase64Encoded" in predictions[0]:
            import base64
            from google.cloud import storage
            img_data = predictions[0]["bytesBase64Encoded"]
            img_bytes = base64.b64decode(img_data)
            bucket_name = f"{GCP_PROJECT_ID}-contextcat-output"
            import time as _time
            ts = int(_time.time())
            blob_name = f"images/clip_{clip_id}_{ts}.png"
            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.upload_from_string(img_bytes, content_type="image/png")
            # Return https:// for GitLab display
            # Veo 3 will convert back to gs:// when needed
            gcs_url = f"https://storage.googleapis.com/{bucket_name}/{blob_name}"
            logger.info(f"Imagen 4 generated image for clip {clip_id}: {gcs_url}")
            return gcs_url

        logger.warning(f"No image data in Imagen 4 response for clip {clip_id}")
        return None

    except requests.exceptions.RequestException as e:
        logger.error(f"Imagen 4 API error for clip {clip_id}: {e}")
        return None


# ===============================================================
# VEO 3: VIDEO + AUDIO GENERATION
# ===============================================================

def build_veo3_prompt(clip: dict, character_tags: str = "") -> str:
    """
    Build a Veo 3 native format prompt from the clip JSON.

    Critical Veo 3 rules:
    - Use "Character says: dialogue" (with colon) to prevent subtitle generation
    - Inject character_tags at the START to lock character appearance
    - Skip character injection for cat-only clips
    - Add negative prompts to reduce common AI errors
    """
    visual = clip.get("visual", "")
    audio = clip.get("audio", {})
    voiceover = audio.get("voiceover", "")
    sfx = audio.get("sfx", "")
    music = audio.get("music", "")

    # Detect clip type
    visual_lower = visual.lower()
    has_human = any(w in visual_lower for w in ["woman", "man", "person", "she", "her"])
    has_cats = "cat" in visual_lower
    has_cats_only = has_cats and not has_human
    has_laptop = any(w in visual_lower for w in ["laptop", "screen", "computer", "monitor", "display"])

    # Inject character tags at the START for human clips
    if character_tags and not has_cats_only:
        base = f"{character_tags}, {QUALITY_SUFFIX}. {visual}"
    else:
        base = visual

    # Auto-inject constraints based on clip content
    if has_laptop and has_human:
        # Scene with laptop: enforce screen facing camera
        parts = [f"{base} {LAPTOP_CONSTRAINT}"]
        logger.info(f"Laptop constraint injected for clip")
    elif has_cats:
        # Cat scene: enforce natural colors + no random screens
        parts = [f"{base} {CATS_CONSTRAINT} {NO_SCREEN_CONSTRAINT}"]
        logger.info(f"Cats + no screen constraint injected for clip")
    else:
        # Human scene without laptop: prevent random screens appearing
        parts = [f"{base} {NO_SCREEN_CONSTRAINT}"]
        logger.info(f"No screen constraint injected for clip")
    if voiceover:
        parts.append(f"A calm voice narrates: {voiceover}")
    if sfx:
        parts.append(f"Audio: {sfx}.")
    if music:
        parts.append(f"Background music: {music}.")
    # Voice consistency guidance
    parts.append(
        "Voiceover spoken by same young Asian woman throughout, "
        "soft and calm voice, early 20s, gentle tone, natural pacing."
    )
    parts.append(
        "No morphing, no distortion, no text overlays, "
        "no watermarks, no subtitle captions, smooth motion."
    )
    return " ".join(parts)


def generate_video_clip(clip: dict, reference_image_data: str | None, clip_id: int, char_reference_uri: str | None = None) -> str | None:
    """
    Generate a video clip with native audio using Veo 3.
    Returns the public HTTPS URL of the generated video in GCS.

    Uses predictLongRunning endpoint because video generation
    takes 60-120 seconds.
    """
    token = get_google_token()
    veo3_prompt = build_veo3_prompt(clip, character_tags=clip.get("_character_tags", ""))
    logger.info(f"Veo 3 prompt for clip {clip_id}: {veo3_prompt[:120]}...")

    endpoint = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/veo-3.1-generate-001:predictLongRunning"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    instance = {"prompt": veo3_prompt}

    def to_gcs(url):
        if url and url.startswith("https://storage.googleapis.com/"):
            return url.replace("https://storage.googleapis.com/", "gs://", 1)
        return url

    # Image-to-video: pass image as first frame
    # referenceImages + image together causes 400 in Veo 3.1, use image only
    if reference_image_data:
        # Clip 2+: last frame of previous clip for seamless chaining
        frame_gcs = to_gcs(reference_image_data)
        instance["image"] = {"gcsUri": frame_gcs, "mimeType": "image/png"}
        logger.info(f"Frame-chaining: first frame = {frame_gcs}")
    elif char_reference_uri:
        # Clip 1: reference image as first frame for character lock
        char_gcs = to_gcs(char_reference_uri)
        instance["image"] = {"gcsUri": char_gcs, "mimeType": "image/png"}
        logger.info(f"Clip 1: reference image as first frame = {char_gcs}")

    payload = {
        "instances": [instance],
        "parameters": {
            "aspectRatio": "16:9",
            "sampleCount": 1,
            "durationSeconds": clip.get("duration", 8),
            "enhancePrompt": True,
            "generateAudio": True,
            "storageUri": f"gs://{GCP_PROJECT_ID}-contextcat-output/"
        }
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        operation = resp.json()
        operation_name = operation.get("name", "")
        if not operation_name:
            logger.error(f"No operation name returned for clip {clip_id}")
            return None
        logger.info(f"Veo 3 operation started for clip {clip_id}: {operation_name}")
        return poll_veo3_operation(operation_name, clip_id)

    except requests.exceptions.RequestException as e:
        logger.error(f"Veo 3 API error for clip {clip_id}: {e}")
        return None


def poll_veo3_operation(operation_name: str, clip_id: int, max_wait: int = 600) -> str | None:
    """
    Poll the long-running Veo 3 operation until it completes.
    Checks every 15 seconds, up to max_wait seconds.
    Falls back to GCS bucket scan if timeout occurs.
    """
    token = get_google_token()
    op_endpoint = (
        f"https://{GCP_LOCATION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/"
        f"publishers/google/models/veo-3.1-generate-001:fetchPredictOperation"
    )
    headers = {"Authorization": f"Bearer {token}"}
    elapsed = 0

    while elapsed < max_wait:
        time.sleep(15)
        elapsed += 15

        # Refresh token every 5 minutes
        if elapsed % 300 == 0:
            token = get_google_token()
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = requests.post(
                op_endpoint,
                headers={**headers, "Content-Type": "application/json"},
                json={"operationName": operation_name},
                timeout=30
            )
            resp.raise_for_status()
            op = resp.json()

            if op.get("done"):
                if "error" in op:
                    logger.error(f"Veo 3 operation failed for clip {clip_id}: {op['error']}")
                    return None
                response = op.get("response", {})
                videos = response.get("videos", [])
                if videos:
                    video_uri = videos[0].get("gcsUri", "")
                    if video_uri:
                        if video_uri.startswith("gs://"):
                            video_uri = video_uri.replace("gs://", "https://storage.googleapis.com/", 1)
                        logger.info(f"Veo 3 generated video for clip {clip_id}: {video_uri}")
                        return video_uri

            logger.info(f"Clip {clip_id}: waiting... ({elapsed}s elapsed)")

        except requests.exceptions.RequestException as e:
            logger.warning(f"Poll error for clip {clip_id}: {e}")

    logger.error(f"Veo 3 operation timed out for clip {clip_id}, trying GCS fallback...")
    try:
        from google.cloud import storage
        storage_client = storage.Client()
        bucket = storage_client.bucket(f"{GCP_PROJECT_ID}-contextcat-output")
        mp4_blobs = [b for b in bucket.list_blobs() if b.name.endswith('.mp4')]
        if mp4_blobs:
            latest = sorted(mp4_blobs, key=lambda b: b.time_created, reverse=True)[0]
            url = f"https://storage.googleapis.com/{GCP_PROJECT_ID}-contextcat-output/{latest.name}"
            logger.info(f"GCS fallback found video: {url}")
            return url
    except Exception as e:
        logger.error(f"GCS fallback failed: {e}")
    return None


# ===============================================================
# FRAME EXTRACTION FOR FRAME-CHAINING
# ===============================================================

def extract_last_frame(video_gcs_uri: str, clip_id: int) -> str | None:
    """
    Extract the last frame of a video clip from GCS.
    Used for frame-chaining: last frame of clip N becomes first frame of clip N+1.
    Returns GCS URI of the extracted frame image.
    """
    try:
        import subprocess
        import tempfile
        from google.cloud import storage

        # Download video from GCS
        storage_client = storage.Client()
        bucket_name = video_gcs_uri.replace("gs://", "").split("/")[0]
        blob_path = "/".join(video_gcs_uri.replace("gs://", "").split("/")[1:])

        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_path)

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_video:
            blob.download_to_filename(tmp_video.name)
            video_path = tmp_video.name

        # Extract last frame using FFmpeg
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_frame:
            frame_path = tmp_frame.name

        result = subprocess.run([
            "ffmpeg", "-sseof", "-0.1", "-i", video_path,
            "-vframes", "1", "-q:v", "2", "-y", frame_path
        ], capture_output=True, timeout=120)

        if result.returncode != 0:
            logger.error(f"FFmpeg failed for clip {clip_id}: {result.stderr.decode()}")
            return None

        # Upload frame to GCS
        import time as _time
        ts = int(_time.time())
        frame_blob_name = f"frames/clip_{clip_id}_lastframe_{ts}.png"
        frame_blob = bucket.blob(frame_blob_name)
        frame_blob.upload_from_filename(frame_path, content_type="image/png")

        frame_gcs_uri = f"gs://{bucket_name}/{frame_blob_name}"
        logger.info(f"Extracted last frame for clip {clip_id}: {frame_gcs_uri}")
        return frame_gcs_uri

    except Exception as e:
        logger.error(f"Frame extraction failed for clip {clip_id}: {e}")
        return None


# ===============================================================
# MAIN PIPELINE ORCHESTRATOR
# ===============================================================

def run_media_pipeline(project_id: int, issue_iid: int):
    """
    Part 1: Read storyboard, generate Imagen 4 reference images, post Gate 1.
    Returns after Gate 1 — does NOT wait inline for user approval.
    User approval via 'approved, generate videos' triggers run_video_pipeline.
    """
    logger.info(f"Starting ContextCat MEDIA pipeline for Issue #{issue_iid}")
    post_issue_comment(
        project_id, issue_iid,
        "ContextCat Cloud Run activated!\n\nReading storyboard from Issue..."
    )

    issue = get_issue(project_id, issue_iid)
    storyboard = extract_storyboard_json(issue)

    if not storyboard:
        post_issue_comment(
            project_id, issue_iid,
            "ContextCat Error: Could not find storyboard JSON in Issue.\n\n"
            "Please make sure Cat-2 has posted a ```storyboard block."
        )
        return

    clips = storyboard.get("clips", [])
    total_duration = storyboard.get("total_duration", 30)
    video_ai = storyboard.get("video_ai", "Veo 3")
    logger.info(f"Found storyboard: {len(clips)} clips, {total_duration}s, {video_ai}")

    post_issue_comment(
        project_id, issue_iid,
        f"Building Story Bible for visual consistency across {len(clips)} clips..."
    )

    project_context = issue.get("description", "")
    bible_result = build_story_bible(clips, project_context)
    story_bible = bible_result["story_bible"]
    character_tags = bible_result["character_tags"]

    post_issue_comment(
        project_id, issue_iid,
        f"Cat-3 (Visual Officer) generating Clip 1 reference image with Imagen 4...\n\n"
        f"_Story Bible built by Gemini. Character locked: {character_tags[:80]}..._"
    )

    # Method A: Only generate Clip 1 reference image (saves cost)
    # Clips 2-4 use last frame of previous clip via frame-chaining
    reference_images = []
    image_results = []

    for i, clip in enumerate(clips):
        clip_id = clip.get("clip_id", i + 1)
        if clip_id == 1:
            logger.info(f"Generating reference image for clip 1 only...")
            img_data = generate_reference_image(clip.get("visual", ""), story_bible, clip_id)
            reference_images.append(img_data)
            if img_data:
                image_results.append(f"- Clip 1: Generated\n  ![ref]({img_data})")
            else:
                image_results.append(f"- Clip 1: Failed")
        else:
            reference_images.append(None)

    import datetime as _dt
    gate1_timestamp = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    checkpoint_msg = (
        "**Cat-3 Complete! Reference images generated.**\n\n"
        "**Story Bible:**\n"
        f"```\n{story_bible}\n```\n\n"
        "**Reference images:**\n"
        + "\n".join(image_results) + "\n\n"
        "---\n"
        "**HUMAN CHECKPOINT 1 - Review before video generation**\n\n"
        "Do the reference images look right for your project?\n\n"
        "Reply `approved, generate videos` to continue\n"
        "Reply `start over` to restart\n\n"
        f"<!-- gate1_posted_at: {gate1_timestamp} -->"
    )
    post_issue_comment(project_id, issue_iid, checkpoint_msg)

    # Gate 1: stop here, wait for webhook re-trigger with approval keyword
    logger.info("Gate 1 posted. Waiting for user approval via webhook.")
    return


def run_single_clip(project_id: int, issue_iid: int, clip_number: int):
    """
    Generate a single video clip and post the last frame to Issue for approval.
    Part of the step-by-step review flow (testing mode).
    clip_number: 1, 2, 3, or 4
    """
    logger.info(f"Starting single clip generation: clip {clip_number} for Issue #{issue_iid}")

    issue = get_issue(project_id, issue_iid)
    storyboard = extract_storyboard_json(issue)
    if not storyboard:
        post_issue_comment(project_id, issue_iid, "Error: Could not find storyboard JSON.")
        return

    clips = storyboard.get("clips", [])
    clip = next((c for c in clips if c.get("clip_id") == clip_number), None)
    if not clip:
        post_issue_comment(project_id, issue_iid, f"Error: Could not find clip {clip_number} in storyboard.")
        return

    # Extract character_tags
    character_tags = FIXED_CHARACTER
    notes = sorted(issue.get("notes", []), key=lambda n: n["created_at"], reverse=True)
    for note in notes:
        body = note.get("body", "")
        if "Character locked:" in body:
            try:
                start = body.index("Character locked:") + len("Character locked:")
                character_tags = body[start:start+200].strip().rstrip("._")
            except Exception:
                pass
            break
    clip["_character_tags"] = character_tags

    # Get reference image for this clip
    bucket_name = f"{GCP_PROJECT_ID}-contextcat-output"

    # For clip 1: use reference image from Gate 1
    # For clip 2+: use last frame of previous clip
    chain_image = None
    if clip_number == 1:
        # Use reference image from GCS
        import re as _re
        for note in notes:
            body = note.get("body", "")
            pattern = rf"https://storage\.googleapis\.com/[^\s\)]+clip_1_\d+\.png"
            match = _re.search(pattern, body)
            if match:
                chain_image = match.group(0)
                break
    else:
        # Find last frame of previous clip from Issue comments
        prev_clip = clip_number - 1
        import re as _re
        for note in notes:
            body = note.get("body", "")
            pattern = rf"https://storage\.googleapis\.com/[^\s\)]+frames/clip_{prev_clip}_lastframe_\d+\.png"
            match = _re.search(pattern, body)
            if match:
                chain_image = match.group(0)
                logger.info(f"Found last frame of clip {prev_clip}: {chain_image}")
                break
        if not chain_image:
            logger.warning(f"No last frame found for clip {prev_clip}, using reference image")
            import re as _re2
            for note in notes:
                body = note.get("body", "")
                pattern = rf"https://storage\.googleapis\.com/[^\s\)]+clip_{clip_number}_\d+\.png"
                match = _re2.search(pattern, body)
                if match:
                    chain_image = match.group(0)
                    break

    post_issue_comment(project_id, issue_iid,
        f"Generating clip {clip_number}/4 with Veo 3.1..."
        + (" _(frame-chained from previous clip)_" if clip_number > 1 and chain_image else ""))

    video_uri = generate_video_clip(clip, chain_image if clip_number > 1 else None, clip_number,
                                     char_reference_uri=chain_image if clip_number == 1 else None)

    if not video_uri:
        post_issue_comment(project_id, issue_iid, f"Clip {clip_number} generation failed.")
        return

    # Extract last frame
    video_gcs = video_uri
    if video_uri.startswith("https://storage.googleapis.com/"):
        video_gcs = video_uri.replace("https://storage.googleapis.com/", "gs://", 1)

    last_frame_uri = extract_last_frame(video_gcs, clip_number)

    # Convert last frame to https for display
    last_frame_https = None
    if last_frame_uri and last_frame_uri.startswith("gs://"):
        last_frame_https = last_frame_uri.replace("gs://", "https://storage.googleapis.com/", 1)

    # Post result to Issue
    if clip_number < 4:
        next_trigger = f"approved clip {clip_number}"
        next_clip = clip_number + 1
        msg = f"**Clip {clip_number} Complete!**\n\nVideo: {video_uri}\n\n"
        if last_frame_https:
            msg += f"**Last frame (will be first frame of Clip {next_clip}):**\n"
            msg += f"![last frame]({last_frame_https})\n\n"
        msg += f"---\nDoes the last frame look right?\n\n"
        msg += f"Reply `{next_trigger}` to generate Clip {next_clip}\n"
        msg += f"Reply `start over` to restart"
    else:
        # Clip 4: final delivery
        import re as _re3
        all_videos = {}
        for note in sorted(notes, key=lambda n: n["created_at"]):
            body = note.get("body", "")
            for cn in range(1, 5):
                if f"Clip {cn} Complete" in body:
                    url_match = _re3.search(r"Video: (https://[^\s\n]+)", body)
                    if url_match:
                        all_videos[cn] = url_match.group(1)
        all_videos[4] = video_uri
        msg = "**ContextCat Delivery Complete!**\n\nAll 4 clips generated!\n\n"
        msg += "| Clip | Video URL |\n|------|----------|\n"
        for cn in range(1, 5):
            url = all_videos.get(cn, "N/A")
            msg += f"| {cn} | {url} |\n"
        msg += "\n_Generated by ContextCat x Claude x Imagen 4 x Veo 3.1 x Google Cloud Run_"
    post_issue_comment(project_id, issue_iid, msg)
    logger.info(f"Clip {clip_number} complete for Issue #{issue_iid}")


def run_video_pipeline(project_id: int, issue_iid: int):
    """
    Part 2: Read storyboard from Issue, generate Veo 3 videos, post Delivery.
    Triggered when user comments 'approved, generate videos'.
    """
    logger.info(f"Starting ContextCat VIDEO pipeline for Issue #{issue_iid}")
    post_issue_comment(
        project_id, issue_iid,
        "ContextCat Part 2 activated!\nReading storyboard and starting Veo 3 generation..."
    )

    issue = get_issue(project_id, issue_iid)
    storyboard = extract_storyboard_json(issue)

    if not storyboard:
        post_issue_comment(project_id, issue_iid, "Error: Could not find storyboard JSON.")
        return

    clips = storyboard.get("clips", [])
    total_duration = storyboard.get("total_duration", 30)
    video_ai = storyboard.get("video_ai", "Veo 3")
    # Extract reference images from Gate 1 comment URLs
    reference_images = [None] * len(clips)
    notes_sorted = sorted(issue.get("notes", []), key=lambda n: n["created_at"], reverse=True)
    for note in notes_sorted:
        body = note.get("body", "")
        if "Cat-3 Complete" in body or "Reference images" in body:
            for i, clip in enumerate(clips):
                clip_id = clip.get("clip_id", i + 1)
                # Find https URL for this clip in the comment
                pattern = rf"https://storage\.googleapis\.com/[^\s\)]+clip_{clip_id}_\d+\.png"
                import re as _re
                match = _re.search(pattern, body)
                if match:
                    https_url = match.group(0)
                    # Convert to gs:// for Veo 3
                    gcs_uri = https_url.replace("https://storage.googleapis.com/", "gs://", 1)
                    reference_images[i] = gcs_uri
                    logger.info(f"Found ref image for clip {clip_id}: {gcs_uri}")
            break
    # Log any missing
    for i, clip in enumerate(clips):
        if not reference_images[i]:
            logger.warning(f"No reference image found for clip {clip.get('clip_id', i+1)}")

    # Extract character_tags from Gate 1 comment in Issue
    character_tags = FIXED_CHARACTER  # fallback
    notes = sorted(issue.get("notes", []), key=lambda n: n["created_at"], reverse=True)
    for note in notes:
        body = note.get("body", "")
        if "Character locked:" in body:
            try:
                start = body.index("Character locked:") + len("Character locked:")
                character_tags = body[start:start+200].strip().rstrip("._")
                logger.info(f"Extracted character_tags from Gate 1: {character_tags[:80]}")
            except Exception:
                pass
            break

    # Inject character_tags into each clip for Veo prompt building
    for clip in clips:
        clip["_character_tags"] = character_tags
    post_issue_comment(
        project_id, issue_iid,
        f"Cat-4 (Audio Director) generating {len(clips)} video clips with Veo 3...\n\n"
        "_This may take 2-5 minutes per clip. Sit tight!_"
    )

    video_results = []
    video_urls = []
    last_frame_uri = None  # For frame-chaining between clips

    for i, clip in enumerate(clips):
        clip_id = clip.get("clip_id", i + 1)

        # Frame-chaining: use last frame of previous clip as first frame
        # But keep reference_image as fallback for clip 1
        if last_frame_uri and i > 0:
            chain_image = last_frame_uri
            logger.info(f"Frame-chaining clip {clip_id}: using last frame of clip {clip_id-1}")
        else:
            chain_image = reference_images[i]
            logger.info(f"Clip {clip_id}: using reference image")

        post_issue_comment(
            project_id, issue_iid,
            f"Generating clip {clip_id}/{len(clips)} with Veo 3.1..."
            + (" _(frame-chained from previous clip)_" if last_frame_uri and i > 0 else "")
        )
        # Clip 1: no previous frame, only use reference image for character lock
        # Clip 2+: use last frame for chaining + reference image for character lock
        if last_frame_uri and i > 0:
            # frame-chaining: chain_image is last frame, reference_images[i] is char ref
            video_uri = generate_video_clip(clip, chain_image, clip_id, char_reference_uri=reference_images[i])
        else:
            # first clip: no chaining, only character lock via referenceImages
            video_uri = generate_video_clip(clip, None, clip_id, char_reference_uri=reference_images[i])
        video_urls.append(video_uri)

        if video_uri:
            video_results.append(f"| {clip_id} | {clip.get('duration', 8)}s | {video_uri} | OK |")
            # Extract last frame for next clip's frame-chaining
            # Convert https:// to gs:// if needed
            video_gcs = video_uri
            if video_uri.startswith("https://storage.googleapis.com/"):
                video_gcs = video_uri.replace("https://storage.googleapis.com/", "gs://", 1)
            last_frame_uri = extract_last_frame(video_gcs, clip_id)
            if last_frame_uri:
                logger.info(f"Frame-chain ready for next clip: {last_frame_uri}")
            else:
                logger.warning(f"Frame extraction failed for clip {clip_id}, next clip will use reference image")
                last_frame_uri = None
        else:
            video_results.append(f"| {clip_id} | {clip.get('duration', 8)}s | Generation failed | FAILED |")
            last_frame_uri = None  # Reset chain on failure

    delivery_msg = (
        "**ContextCat Delivery Complete!**\n\n"
        "## Your Video Production Package\n\n"
        f"**Format:** {len(clips)} clips x {clips[0].get('duration', 8)}s = {total_duration}s | {video_ai}\n\n"
        "---\n\n"
        "### Video Clips (with audio)\n\n"
        "| Clip | Duration | Video URL | Status |\n"
        "|------|----------|-----------|--------|\n"
        + "\n".join(video_results) + "\n\n"
        "---\n\n"
        "_Generated by ContextCat x Claude (Anthropic) x Imagen 4 x Veo 3 x Google Cloud Run_"
    )
    post_issue_comment(project_id, issue_iid, delivery_msg)
    logger.info(f"ContextCat VIDEO pipeline complete for Issue #{issue_iid}")


# ===============================================================
# FLASK WEBHOOK ENDPOINT
# ===============================================================

@app.route("/webhook", methods=["POST"])
def gitlab_webhook():
    """
    Receives GitLab webhook events (Issue Note created).

    STRICT TRIGGER RULES:
    - "contextcat generate media"  ->  runs Imagen 4 + Gate 1
    - "approved, generate videos"  ->  runs Veo 3 + Delivery

    ALL other comments are ignored, including:
    - storyboard JSON comments (this was the bug fixed in v1.1.0)
    - service account comments (ai-contextcat-*)
    - general discussion comments
    """
    # Verify webhook secret
    token = request.headers.get("X-Gitlab-Token", "")
    if WEBHOOK_SECRET and token != WEBHOOK_SECRET:
        logger.warning("Invalid webhook secret")
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "No payload"}), 400

    # Only handle Note events on Issues
    event_type = payload.get("object_kind")
    if event_type != "note":
        return jsonify({"status": "ignored", "reason": "not a note event"}), 200

    noteable_type = payload.get("object_attributes", {}).get("noteable_type")
    if noteable_type != "Issue":
        return jsonify({"status": "ignored", "reason": "not an issue note"}), 200

    # Extract details
    project_id = payload.get("project", {}).get("id")
    issue_iid = payload.get("issue", {}).get("iid")
    note_body = payload.get("object_attributes", {}).get("note", "")
    comment_body = note_body.lower()

    if not project_id or not issue_iid:
        return jsonify({"error": "Missing project_id or issue_iid"}), 400

    # Always ignore service account comments (prevents infinite loop)
    author_username = payload.get("user", {}).get("username", "")
    if author_username.startswith("ai-contextcat"):
        logger.info(f"Ignored service account comment from: {author_username}")
        return jsonify({"status": "ignored", "reason": "service account comment"}), 200

    # Strict triggers — nothing else can start the pipeline
    TRIGGER_GENERATE = "contextcat generate media"
    TRIGGER_APPROVE  = "approved, generate videos"

    if TRIGGER_GENERATE in comment_body:
        trigger = "generate"
    elif comment_body.strip() == TRIGGER_APPROVE or comment_body.strip().startswith(TRIGGER_APPROVE + "\n") or comment_body.strip().endswith("\n" + TRIGGER_APPROVE):
        trigger = "approve"
    elif "approved clip 1" in comment_body:
        trigger = "clip2"
    elif "approved clip 2" in comment_body:
        trigger = "clip3"
    elif "approved clip 3" in comment_body:
        trigger = "clip4"
    else:
        logger.info(f"No trigger phrase. Comment preview: {note_body[:80]}")
        return jsonify({"status": "ignored", "reason": "no trigger phrase found"}), 200

    # Timestamp lock for approval: only accept comments NEWER than Gate 1
    if trigger == "approve":
        comment_time = payload.get("object_attributes", {}).get("created_at", "")
        logger.info(f"Gate1 check: comment_time={repr(comment_time)}")
        # Find the latest Gate 1 comment time from GitLab API
        try:
            headers_gl = {"PRIVATE-TOKEN": GITLAB_TOKEN}
            notes_url = f"{GITLAB_URL}/api/v4/projects/{project_id}/issues/{issue_iid}/notes"
            notes_resp = requests.get(notes_url, headers=headers_gl, params={"per_page": 100}, timeout=15)
            notes_resp.raise_for_status()
            notes = notes_resp.json()
            def normalize_time(t):
                return t.replace("T", " ").replace("Z", "").split(".")[0].strip()

            ct = normalize_time(comment_time)

            # Find the MOST RECENT Gate 1 comment
            gate1_time = None
            for note in sorted(notes, key=lambda n: n["created_at"], reverse=True):
                body = note.get("body", "")
                if "gate1_posted_at:" in body or "HUMAN CHECKPOINT 1" in body or "Cat-3 Complete" in body:
                    gate1_time = normalize_time(note["created_at"])
                    break

            # Find the MOST RECENT "contextcat generate media" trigger comment
            trigger_time = None
            for note in sorted(notes, key=lambda n: n["created_at"], reverse=True):
                body = note.get("body", "").lower()
                if "contextcat generate media" in body:
                    trigger_time = normalize_time(note["created_at"])
                    break

            if not gate1_time:
                logger.info("No Gate 1 found, rejecting approval")
                return jsonify({"status": "ignored", "reason": "no gate1 found"}), 200

            # approved comment must be NEWER than both Gate 1 AND the trigger
            # Must be strictly AFTER gate1 by at least 1 second
            from datetime import datetime as _dt2
            try:
                logger.info(f"Comparing: ct={repr(ct)} gate1={repr(gate1_time)}")
                ct_dt = _dt2.strptime(ct, "%Y-%m-%d %H:%M:%S")
                g1_dt = _dt2.strptime(gate1_time, "%Y-%m-%d %H:%M:%S")
                diff = (ct_dt - g1_dt).total_seconds()
                logger.info(f"Time diff: {diff}s")
                if diff <= 60:
                    logger.info(f"Rejected: approval too close to gate1 (diff={diff}s)")
                    return jsonify({"status": "ignored", "reason": "approval too close to gate1"}), 200
            except Exception as e:
                logger.warning(f"Timestamp parse error: {e}, ct={repr(ct)}, gate1={repr(gate1_time)}")
                if ct <= gate1_time:
                    logger.info(f"Rejected: approval {ct} <= gate1 {gate1_time}")
                    return jsonify({"status": "ignored", "reason": "stale approval"}), 200

            if trigger_time and ct <= trigger_time:
                logger.info(f"Rejected: approval {ct} <= trigger {trigger_time}")
                return jsonify({"status": "ignored", "reason": "stale approval"}), 200

            logger.info(f"Timestamp OK: approval {ct} > gate1 {gate1_time}")
        except Exception as e:
            logger.warning(f"Timestamp check failed (proceeding anyway): {e}")

    # Timestamp protection for clip triggers (prevent duplicate triggers)
    if trigger in ["clip2", "clip3", "clip4"]:
        clip_num = {"clip2": 1, "clip3": 2, "clip4": 3}[trigger]
        comment_time = payload.get("object_attributes", {}).get("created_at", "")
        try:
            headers_gl = {"PRIVATE-TOKEN": GITLAB_TOKEN}
            notes_url = f"{GITLAB_URL}/api/v4/projects/{project_id}/issues/{issue_iid}/notes"
            notes_resp = requests.get(notes_url, headers=headers_gl, params={"per_page": 100}, timeout=15)
            notes_resp.raise_for_status()
            notes = notes_resp.json()
            def normalize_time(t):
                return t.replace("T", " ").replace("Z", "").split(".")[0].strip()
            ct = normalize_time(comment_time)
            # Find the most recent "Clip N Complete" comment
            clip_complete_time = None
            for note in sorted(notes, key=lambda n: n["created_at"], reverse=True):
                if f"Clip {clip_num} Complete" in note.get("body", ""):
                    clip_complete_time = normalize_time(note["created_at"])
                    break
            if clip_complete_time:
                from datetime import datetime as _dt3
                ct_dt = _dt3.strptime(ct, "%Y-%m-%d %H:%M:%S")
                cc_dt = _dt3.strptime(clip_complete_time, "%Y-%m-%d %H:%M:%S")
                diff = (ct_dt - cc_dt).total_seconds()
                if diff <= 60:
                    logger.info(f"Rejected duplicate clip trigger: diff={diff}s")
                    return jsonify({"status": "ignored", "reason": "duplicate clip trigger"}), 200
        except Exception as e:
            logger.warning(f"Clip timestamp check failed: {e}")

    logger.info(f"Trigger '{trigger}' for project {project_id}, issue #{issue_iid}")

    # Return 200 IMMEDIATELY so GitLab doesn't timeout (GitLab waits max 10s)
    # Pipeline runs in background thread
    def run_in_background():
        try:
            if trigger == "approve":
                run_video_pipeline(project_id, issue_iid)
            else:
                run_media_pipeline(project_id, issue_iid)
        except Exception as e:
            logger.error(f"Background pipeline error: {e}")

    thread = threading.Thread(target=run_in_background)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "accepted", "message": "Pipeline started in background"}), 200


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint for Cloud Run."""
    return jsonify({
        "status": "healthy",
        "service": "ContextCat Media Generation",
        "version": "1.1.0"
    }), 200


@app.route("/", methods=["GET"])
def index():
    """Root endpoint."""
    return jsonify({
        "service": "ContextCat Cloud Run",
        "description": "AI Video Production Pipeline Bridge",
        "endpoints": {
            "/webhook": "GitLab webhook receiver (POST)",
            "/health": "Health check (GET)"
        }
    }), 200


# Entry point
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
