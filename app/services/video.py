import glob
import itertools
import io
import os
import random
import gc
import shutil
import subprocess
from contextlib import redirect_stdout
from typing import List
from loguru import logger
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import Image, ImageFont

from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services.utils import video_effects
from app.utils import file_security, utils

class SubClippedVideoClip:
    def __init__(self, file_path, start_time=None, end_time=None, width=None, height=None, duration=None):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
# Docker 里的 ffmpeg/AAC 组合在默认配置下更容易出现音频质量波动，
# 这里显式抬高音频码率，避免成片阶段因为默认值过低而引入明显失真。
audio_bitrate = "192k"
video_codec = "libx264"
fps = 30
_BGM_EXTENSIONS = (".mp3",)


def get_ffmpeg_binary():
    # 优先复用用户在 config.toml / 环境变量里显式指定的 ffmpeg，可避免
    # Windows 便携包、Docker、自定义安装目录等场景下 PATH 不一致。
    configured_ffmpeg = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if configured_ffmpeg:
        return configured_ffmpeg

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled_ffmpeg:
            return bundled_ffmpeg
    except Exception as exc:
        logger.warning(f"failed to resolve bundled ffmpeg binary: {str(exc)}")

    return "ffmpeg"


def _escape_ffmpeg_concat_path(file_path: str) -> str:
    # concat demuxer 使用单引号包裹路径，路径中的单引号需要先转义。
    return file_path.replace("'", "'\\''")


def concat_video_clips_with_ffmpeg(
    clip_files: List[str], output_file: str, threads: int, output_dir: str
):
    concat_list_file = os.path.join(output_dir, "ffmpeg-concat-list.txt")
    with open(concat_list_file, "w", encoding="utf-8") as fp:
        for clip_file in clip_files:
            absolute_path = os.path.abspath(clip_file)
            fp.write(f"file '{_escape_ffmpeg_concat_path(absolute_path)}'\n")

    command = [
        get_ffmpeg_binary(),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list_file,
        "-c:v",
        video_codec,
        "-threads",
        str(threads or 2),
        "-pix_fmt",
        "yuv420p",
        output_file,
    ]

    try:
        # 使用 ffmpeg 只做一次串联与编码，避免 MoviePy 逐段合并时反复重编码，
        # 从而降低画质劣化与颜色偏移风险。
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg concat failed")
    finally:
        delete_files(concat_list_file)


def _sanitize_image_file(image_path: str) -> str:
    # 某些本地图片虽然能被 Pillow 打开，但会因为损坏的 EXIF/eXIf 元数据导致
    # ImageClip 在解析阶段直接抛异常。这里重新导出一份“干净图片”，把坏元数据剥离掉。
    image_root, _ = os.path.splitext(image_path)
    sanitized_path = f"{image_root}.sanitized.png"

    with Image.open(image_path) as image:
        image.load()
        # 统一导出为 PNG，避免 JPEG/PNG 不同元数据路径继续把坏块带过去。
        cleaned_image = Image.new(image.mode, image.size)
        cleaned_image.putdata(list(image.getdata()))
        cleaned_image.save(sanitized_path)

    return sanitized_path


def _open_image_clip_with_fallback(image_path: str):
    # 优先直接打开原始图片；如果因为损坏元数据失败，再尝试生成无元数据副本。
    try:
        return ImageClip(image_path), image_path
    except Exception as exc:
        logger.warning(
            f"failed to open image directly, trying sanitized copy: {image_path}, error: {str(exc)}"
        )
        sanitized_path = _sanitize_image_file(image_path)
        return ImageClip(sanitized_path), sanitized_path


def _open_video_clip_quietly(video_path: str, audio: bool = False) -> VideoFileClip:
    """
    安静地打开视频文件，避免 MoviePy 2.1.x 把 ffmpeg 探测信息直接打印到 stdout。

    背景：
    当前依赖版本的 `FFMPEG_VideoReader` 内部存在 `print(self.infos)` 和
    `print(ffmpeg command)`，读取无音轨的中间视频时会输出
    `audio_found: False`。这只是输入素材 metadata，不代表最终成片没有音频，
    但会误导 WebUI/终端用户以为生成失败。

    实现：
    1. 只在打开 VideoFileClip 的短窗口内重定向 stdout；
    2. 默认 `audio=False`，因为项目视频素材阶段不需要保留素材原声，
       最终音频会在 `generate_video()` 阶段统一挂载；
    3. 如果依赖库确实输出了内容，降级为 debug 日志，便于必要时排查。
    """
    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        clip = VideoFileClip(video_path, audio=audio)

    moviepy_stdout = captured_stdout.getvalue().strip()
    if moviepy_stdout:
        logger.debug(
            "suppressed MoviePy video reader stdout for "
            f"{video_path}, chars: {len(moviepy_stdout)}"
        )

    return clip


def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]

    for file in files:
        try:
            os.remove(file)
        except Exception as e:
            logger.debug(f"failed to delete file {file}: {str(e)}")

def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file:
        song_dir = utils.song_dir()
        try:
            resolved_bgm_file = file_security.resolve_path_within_directory(
                song_dir, bgm_file
            )
        except ValueError as exc:
            # API 请求里的 bgm_file 来自用户输入，不能直接把任意绝对路径交给
            # MoviePy 打开。这里强制限制到 resource/songs 目录，阻止读取
            # /etc/passwd、配置文件、密钥等非背景音乐文件。
            logger.warning(
                f"reject unsafe bgm file: {bgm_file}, song_dir: {song_dir}, error: {str(exc)}"
            )
            return ""

        if not resolved_bgm_file.lower().endswith(_BGM_EXTENSIONS):
            logger.warning(f"reject unsupported bgm file extension: {resolved_bgm_file}")
            return ""

        return resolved_bgm_file

    if bgm_type == "random":
        suffix = "*.mp3"
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, suffix))
        # 当背景音乐目录为空时，直接回退为“不使用 BGM”，避免 random.choice([]) 抛异常。
        if not files:
            logger.warning(f"no bgm files found in song directory: {song_dir}")
            return ""
        return random.choice(files)

    return ""


def get_video_duration_ffmpeg(video_path: str) -> float:
    command = [
        get_ffmpeg_binary().replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"failed to get video duration: {str(e)}")
        return 0.0

def get_video_size_ffmpeg(video_path: str):
    command = [
        get_ffmpeg_binary().replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        video_path
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        parts = result.stdout.strip().split(',')
        return int(parts[0]), int(parts[1])
    except Exception as e:
        logger.error(f"failed to get video size: {str(e)}")
        return 0, 0

def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
) -> str:
    audio_duration = voice.get_audio_duration(audio_file)
    logger.info(f"audio duration: {audio_duration} seconds")
    logger.info(f"maximum clip duration: {max_clip_duration} seconds")

    transition_value = getattr(video_transition_mode, "value", video_transition_mode)
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    subclipped_items = []
    for video_path in video_paths:
        clip_duration = get_video_duration_ffmpeg(video_path)
        clip_w, clip_h = get_video_size_ffmpeg(video_path)
        
        start_time = 0
        while start_time < clip_duration:
            end_time = min(start_time + max_clip_duration, clip_duration)
            if end_time > start_time:
                subclipped_items.append(
                    SubClippedVideoClip(
                        file_path=video_path,
                        start_time=start_time,
                        end_time=end_time,
                        width=clip_w,
                        height=clip_h,
                        duration=end_time - start_time
                    )
                )
            start_time = end_time
            if video_concat_mode.value == VideoConcatMode.sequential.value:
                break

    if video_concat_mode.value == VideoConcatMode.random.value:
        random.shuffle(subclipped_items)
        
    processed_clip_files = []
    video_duration = 0
    
    # Process clips and loop if necessary
    idx = 0
    while video_duration < audio_duration:
        if not subclipped_items:
            break
        
        item = subclipped_items[idx % len(subclipped_items)]
        idx += 1

        temp_clip = os.path.join(output_dir, f"temp_clip_{idx}.mp4")

        # FFmpeg command to clip, resize, and pad
        filter_complex = f"scale={video_width}:{video_height}:force_original_aspect_ratio=increase,crop={video_width}:{video_height}"

        # Add transitions if needed (simplified for direct ffmpeg)
        # For now, we keep the basic scaling and clipping

        command = [
            get_ffmpeg_binary(),
            "-y",
            "-ss", str(item.start_time),
            "-t", str(item.duration),
            "-i", item.file_path,
            "-vf", filter_complex,
            "-c:v", video_codec,
            "-threads", str(threads),
            "-an", # Remove audio
            temp_clip
        ]
        
        try:
            # Run FFmpeg and capture output for progress
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            for line in process.stdout:
                # We can parse time=... here for granular progress if needed
                pass
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command)
                
            processed_clip_files.append(temp_clip)
            video_duration += item.duration
        except Exception as e:
            logger.error(f"ffmpeg processing failed for {item.file_path}: {str(e)}")

    if not processed_clip_files:
        logger.error("no clips were processed")
        return combined_video_path

    concat_video_clips_with_ffmpeg(
        clip_files=processed_clip_files,
        output_file=combined_video_path,
        threads=threads,
        output_dir=output_dir,
    )
    
    delete_files(processed_clip_files)
    logger.info("video combining completed with ffmpeg direct")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # Create ImageFont
    font = ImageFont.truetype(font, fontsize)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    processed = True

    _wrapped_lines_ = []
    words = text.split(" ")
    _txt_ = ""
    for word in words:
        _before = _txt_
        _txt_ += f"{word} "
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            if _txt_.strip() == word.strip():
                processed = False
                break
            _wrapped_lines_.append(_before)
            _txt_ = f"{word} "
    _wrapped_lines_.append(_txt_)
    if processed:
        _wrapped_lines_ = [line.strip() for line in _wrapped_lines_]
        result = "\n".join(_wrapped_lines_).strip()
        height = len(_wrapped_lines_) * height
        return result, height

    _wrapped_lines_ = []
    chars = list(text)
    _txt_ = ""
    for word in chars:
        _txt_ += word
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            _wrapped_lines_.append(_txt_)
            _txt_ = ""
    _wrapped_lines_.append(_txt_)
    result = "\n".join(_wrapped_lines_).strip()
    height = len(_wrapped_lines_) * height
    return result, height


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
    task_id: str = None,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video with ffmpeg: {video_width} x {video_height}")

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

    # Build FFmpeg command for final video assembly
    # We use complex filter for audio mixing and subtitle overlay

    inputs = ["-i", video_path, "-i", audio_path]

    # Audio filters
    # [1:a] is voice, [2:a] is bgm
    filter_complex = f"[1:a]volume={params.voice_volume}[voice];"

    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file:
        inputs.extend(["-i", bgm_file])
        filter_complex += f"[2:a]volume={params.bgm_volume},aloop=loop=-1:size=2e9[bgm];[voice][bgm]amix=inputs=2:duration=first[aout]"
    else:
        filter_complex += "[voice]anull[aout]"

    # Video filters (Subtitles)
    # Note: subtitles filter in ffmpeg needs specific path escaping
    if subtitle_path and os.path.exists(subtitle_path):
        escaped_subtitle_path = subtitle_path.replace("\\", "/").replace(":", "\\:")
        # Simplified style for direct ffmpeg subtitles filter
        # In a real scenario, we'd need to convert SRT to ASS for advanced styling like MoviePy
        video_filter = f"subtitles='{escaped_subtitle_path}':force_style='FontSize={params.font_size},PrimaryColour={params.text_fore_color.replace('#', '&H00')}'"
    else:
        video_filter = "copy"

    command = [
        get_ffmpeg_binary(),
        "-y",
    ]
    command.extend(inputs)
    command.extend([
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", video_codec,
        "-c:a", audio_codec,
        "-b:a", audio_bitrate,
        "-threads", str(params.n_threads or utils.get_optimal_threads()),
        "-shortest",
        output_file
    ])

    # If we have advanced subtitle styling, we'd add more to the video filter
    if video_filter != "copy":
        # Insert video filter before output file
        command.insert(-1, "-vf")
        command.insert(-1, video_filter)

    try:
        from app.services import state as sm
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

        duration = voice.get_audio_duration(audio_path)

        for line in process.stdout:
            if "time=" in line:
                # Parse time=00:00:01.23
                match = re.search(r"time=(\d+:\d+:\d+\.\d+)", line)
                if match and task_id and duration > 0:
                    time_str = match.group(1)
                    h, m, s = time_str.split(':')
                    current_time = int(h) * 3600 + int(m) * 60 + float(s)
                    progress = 50 + (current_time / duration) * 50
                    sm.state.update_task(task_id, progress=progress)

        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)

        logger.success(f"video generated successfully: {output_file}")
    except Exception as e:
        logger.error(f"ffmpeg video generation failed: {str(e)}")


def preprocess_video(materials: List[MaterialInfo], clip_duration=4):
    # WebUI 在某些二次生成场景下可能传入空素材列表，这里直接返回空结果，避免抛出 NoneType 异常。
    if not materials:
        return []

    # 仅返回通过预处理校验的素材，避免低分辨率图片继续进入后续的视频合成流程。
    valid_materials = []
    local_videos_dir = utils.storage_dir("local_videos", create=True)

    for material in materials:
        if not material.url:
            continue

        try:
            material_source_path = file_security.resolve_path_within_directory(
                local_videos_dir, material.url
            )
        except ValueError as exc:
            # local video_source 的素材路径来自 API 参数，必须限制在专用素材目录。
            # 允许用户传文件名，也兼容历史返回的绝对路径，但不允许逃逸到系统
            # 其他目录，避免任意文件读取或通过 MoviePy 探测本地敏感文件。
            logger.warning(
                f"skip unsafe local material: {material.url}, "
                f"local_videos_dir: {local_videos_dir}, error: {str(exc)}"
            )
            continue

        ext = utils.parse_extension(material_source_path)
        try:
            # 图片素材直接按图片方式读取，避免先走 VideoFileClip 误判后触发不稳定的回退分支。
            if ext in const.FILE_TYPE_IMAGES:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            else:
                clip = _open_video_clip_quietly(material_source_path)
        except Exception:
            # 非标准扩展名或探测失败时再回退到图片模式，兼容历史上直接传本地图片路径的情况。
            try:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            except Exception as exc:
                logger.warning(
                    f"skip unreadable local material: {material.url}, error: {str(exc)}"
                )
                continue
        try:
            width = clip.size[0]
            height = clip.size[1]
            if width < 480 or height < 480:
                logger.warning(f"low resolution material: {width}x{height}, minimum 480x480 required")
                # 探测到低分辨率素材后立即关闭资源，并且不要把该素材返回给后续流程。
                close_clip(clip)
                continue

            if ext in const.FILE_TYPE_IMAGES:
                logger.info(f"processing image with ffmpeg: {material_source_path}")
                close_clip(clip)

                video_file = f"{material_source_path}.mp4"

                # FFmpeg Ken Burns effect (zoom pan)
                # Zoom from 1.0 to 1.1 over the duration
                vf = f"scale=8000:-1,zoompan=z='min(zoom+0.0015,1.1)':d={30*clip_duration}:s={width}x{height}:fps=30,scale={width}:{height}"

                command = [
                    get_ffmpeg_binary(),
                    "-y",
                    "-loop", "1",
                    "-i", material_source_path,
                    "-vf", vf,
                    "-t", str(clip_duration),
                    "-c:v", video_codec,
                    "-pix_fmt", "yuv420p",
                    "-threads", str(utils.get_optimal_threads()),
                    video_file
                ]

                subprocess.run(command, capture_output=True, check=True)
                material.url = video_file
                logger.success(f"image processed: {video_file}")
            else:
                # 普通视频素材只需要读取尺寸做校验，校验完成后立即释放句柄即可。
                close_clip(clip)
        except Exception:
            close_clip(clip)
            raise

        valid_materials.append(material)

    return valid_materials
