from __future__ import annotations

import base64
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from openai import OpenAI


ROOT = Path(r"D:\CODE\Semester-3\PeerTrain")
WORK_DIR = ROOT / ".codex_work" / "literature_translation"
MANIFEST_PATH = WORK_DIR / "manifest.json"
PAGE_MARKER_RE = re.compile(r"<<<PAGE_\d{4}>>>")
VISUAL_MARKER_RE = re.compile(r"<<<PAGE_\d{4}_VISUAL>>>")
WRITE_LOCK = threading.Lock()

TRANSLATION_INSTRUCTIONS = """你是一名严谨的英译中学术翻译者。你的唯一任务是把给出的英文论文内容完整翻译为简体中文。

硬性要求：
1. 不摘要、不删减、不合并论证、不补写内容，不输出任何说明性前言或结语。
2. 原样保留所有 <<<PAGE_0001>>> 形式的页码标记，数量、顺序和字符必须完全一致，并让每个标记单独成行。
3. 保留作者姓名、年份、文内引文、引用序号、DOI、URL、模型名、数据集名、变量名、统计量、公式和单位；不要改写数字。
4. 翻译标题、章节标题、正文、脚注、图注和表注。参考文献表中的书目信息保持英文原貌，以免破坏检索。
5. 尽量保留原有段落、标题、项目符号与表格文本的行列关系；不要使用 Markdown 代码围栏。
6. 遇到断行和软连字符时，按正常英文词义还原后翻译；不要逐行机械直译。
7. 使用自然、准确、可用于研究阅读的中文；对于不确定的术语保留英文括注。

统一术语：
- peer support worker = 同伴支持工作者
- peer supporter = 同伴支持者
- peer counselor = 同伴咨询员
- lived experience = 亲历经验
- recovery-oriented = 康复导向
- boundary / boundaries = 边界
- role-play = 角色扮演
- formative feedback = 形成性反馈
- simulated patient = 模拟患者
- virtual patient = 虚拟患者
- help-seeker = 求助者
- scaffolding = 脚手架式支持
- agency = 自主性
- relationship = 关系
- safety = 安全
"""

VISUAL_INSTRUCTIONS = """你是一名严谨的英译中学术翻译者。图片来自英文论文的某一页。

只处理页面中图表、流程图、界面截图、问卷、附录图片或其他无法由 PDF 文本层提取的可见英文。逐项转写其英文标签并给出准确简体中文译文。普通连续正文通常已由另一流程翻译，除非整页只有图片，否则不要重复大段正文。保留数字、公式、统计量、按钮名称、数据集名和模型名。不要猜测看不清的文字，用“[无法辨认]”标记。不要输出 Markdown 代码围栏。

输出必须以指定的 <<<PAGE_XXXX_VISUAL>>> 标记开头，然后按“英文：……\n中文：……”的成对形式列出；若页面没有需要补译的图像文字，明确写“未发现需要补译的图像文字”。
"""


@dataclass(frozen=True)
class Job:
    kind: str
    number: int
    key: str
    title_en: str
    payload: dict[str, Any]
    corpus_file: Path
    translation_file: Path


def _client() -> OpenAI:
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=settings.OPENAI_API_KEY, timeout=600.0, max_retries=4)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_result(path: Path, corpus: dict[str, Any], model: str) -> dict[str, Any]:
    if path.exists():
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("source_sha256") != corpus.get("source_sha256"):
            raise RuntimeError(f"Source changed for {path.name}; delete stale translation result")
        current_chunk_ids = {str(chunk["chunk_id"]) for chunk in corpus["chunks"]}
        current_visual_ids = {str(visual["page"]) for visual in corpus.get("visuals", [])}
        result["chunks"] = {
            key: value
            for key, value in result.get("chunks", {}).items()
            if key in current_chunk_ids
        }
        result["visuals"] = {
            key: value
            for key, value in result.get("visuals", {}).items()
            if key in current_visual_ids
        }
        return result
    return {
        "number": corpus["number"],
        "title_en": corpus["title_en"],
        "title_zh": corpus["title_zh"],
        "source_sha256": corpus["source_sha256"],
        "model": model,
        "translation_kind": "AI-assisted full-text Chinese reading translation",
        "chunks": {},
        "visuals": {},
    }


def _usage_dict(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _translate_text(job: Job, client: OpenAI, model: str) -> dict[str, Any]:
    source_text = str(job.payload["source_text"])
    expected_markers = PAGE_MARKER_RE.findall(source_text)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.responses.create(
                model=model,
                instructions=TRANSLATION_INSTRUCTIONS,
                input=(
                    f"论文原标题：{job.title_en}\n"
                    f"当前内容块：{job.key}\n\n"
                    f"{source_text}"
                ),
                reasoning={"effort": "none"},
                max_output_tokens=24_000,
                store=False,
                metadata={"task": "peertrain_reference_translation", "document": f"{job.number:02d}", "chunk": job.key},
            )
            output = (response.output_text or "").strip()
            if not output:
                raise ValueError("Empty translation")
            if expected_markers:
                # Each request contains exactly one source page. The model may omit or
                # relocate a bookkeeping marker even when the translation is complete;
                # normalize it deterministically rather than paying for a retry.
                output = PAGE_MARKER_RE.sub("", output).strip()
                output = f"{expected_markers[0]}\n{output}".rstrip()
            return {
                "chunk_id": job.key,
                "pages": job.payload["pages"],
                "source_sha256": job.payload["source_sha256"],
                "translation": output,
                "response_id": response.id,
                "usage": _usage_dict(response),
            }
        except Exception as exc:  # retry API and structural failures
            last_error = exc
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(f"Text translation failed for {job.number:02d}/{job.key}: {last_error}")


def _translate_visual(job: Job, client: OpenAI, model: str) -> dict[str, Any]:
    page = int(job.payload["page"])
    marker = f"<<<PAGE_{page:04d}_VISUAL>>>"
    image_path = Path(str(job.payload["image_path"]))
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.responses.create(
                model=model,
                instructions=VISUAL_INSTRUCTIONS,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    f"论文原标题：{job.title_en}\n"
                                    f"原文页码：{page}\n"
                                    f"请以 {marker} 开头。"
                                ),
                            },
                            {
                                "type": "input_image",
                                "image_url": f"data:image/jpeg;base64,{encoded}",
                                "detail": "high",
                            },
                        ],
                    }
                ],
                reasoning={"effort": "none"},
                max_output_tokens=8_000,
                store=False,
                metadata={"task": "peertrain_reference_visual_translation", "document": f"{job.number:02d}", "page": str(page)},
            )
            output = (response.output_text or "").strip()
            if not output:
                raise ValueError("Empty visual translation")
            output = VISUAL_MARKER_RE.sub("", output).strip()
            output = f"{marker}\n{output}".rstrip()
            return {
                "page": page,
                "image_path": str(image_path),
                "image_sha256": job.payload["image_sha256"],
                "translation": output,
                "response_id": response.id,
                "usage": _usage_dict(response),
            }
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(f"Visual translation failed for {job.number:02d}/p{page}: {last_error}")


def _run_job(job: Job, model: str) -> tuple[Job, dict[str, Any]]:
    client = _client()
    if job.kind == "text":
        return job, _translate_text(job, client, model)
    return job, _translate_visual(job, client, model)


def main(
    numbers: set[int] | None = None,
    max_chunks: int | None = None,
    workers: int = 4,
) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    model = settings.OPENAI_MODEL or "gpt-5.4-mini"
    document_states: dict[int, tuple[dict[str, Any], dict[str, Any], Path]] = {}
    jobs: list[Job] = []

    for entry in manifest["documents"]:
        number = int(entry["number"])
        if numbers is not None and number not in numbers:
            continue
        corpus_file = Path(entry["corpus_file"])
        translation_file = Path(entry["translation_file"])
        corpus = json.loads(corpus_file.read_text(encoding="utf-8"))
        result = _load_result(translation_file, corpus, model)
        document_states[number] = (corpus, result, translation_file)

        for chunk in corpus["chunks"]:
            key = str(chunk["chunk_id"])
            prior = result["chunks"].get(key)
            if prior and prior.get("source_sha256") == chunk.get("source_sha256"):
                continue
            jobs.append(
                Job(
                    kind="text",
                    number=number,
                    key=key,
                    title_en=corpus["title_en"],
                    payload=chunk,
                    corpus_file=corpus_file,
                    translation_file=translation_file,
                )
            )

        for visual in corpus.get("visuals", []):
            key = str(visual["page"])
            prior = result["visuals"].get(key)
            if prior and prior.get("image_sha256") == visual.get("image_sha256"):
                continue
            jobs.append(
                Job(
                    kind="visual",
                    number=number,
                    key=key,
                    title_en=corpus["title_en"],
                    payload=visual,
                    corpus_file=corpus_file,
                    translation_file=translation_file,
                )
            )

    if max_chunks is not None:
        jobs = jobs[:max_chunks]
    if not jobs:
        print("No pending translation jobs.")
        return

    print(f"Starting {len(jobs)} jobs with model={model}, workers={workers}", flush=True)
    completed = 0
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_run_job, job, model): job for job in jobs}
        for future in as_completed(future_map):
            job = future_map[future]
            try:
                finished_job, payload = future.result()
            except Exception as exc:
                failed.append(f"{job.number:02d}/{job.kind}/{job.key}: {exc}")
                print(f"FAILED {failed[-1]}", flush=True)
                continue
            corpus, result, translation_file = document_states[finished_job.number]
            with WRITE_LOCK:
                if finished_job.kind == "text":
                    result["chunks"][finished_job.key] = payload
                else:
                    result["visuals"][finished_job.key] = payload
                _atomic_write(translation_file, result)
            completed += 1
            usage = payload.get("usage", {})
            print(
                f"[{completed}/{len(jobs)}] {finished_job.number:02d} "
                f"{finished_job.kind} {finished_job.key} "
                f"tokens={usage.get('total_tokens', 0)}",
                flush=True,
            )

    total_input = total_output = total_tokens = 0
    for _, result, _ in document_states.values():
        for group_name in ("chunks", "visuals"):
            for payload in result[group_name].values():
                usage = payload.get("usage", {})
                total_input += int(usage.get("input_tokens", 0))
                total_output += int(usage.get("output_tokens", 0))
                total_tokens += int(usage.get("total_tokens", 0))
    print(
        json.dumps(
            {
                "completed_jobs": completed,
                "input_tokens_recorded": total_input,
                "output_tokens_recorded": total_output,
                "total_tokens_recorded": total_tokens,
                "failed_jobs": len(failed),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if failed:
        raise RuntimeError("Translation jobs failed:\n" + "\n".join(failed))
