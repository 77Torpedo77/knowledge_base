"""Phase 2: LLM 语义提取 — 调用 DeepSeek 进行 section 分段和实体提取"""

import json
import logging

from openai import OpenAI

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a rigorous academic paper structure analyzer. You will receive a JSON array of text blocks, each with a unique "id" and "text" field. These blocks are raw lines from a paper's Markdown — headers may be missing, formatting may be garbled.

YOUR TASK: Analyze the paper blocks and output a JSON object with two fields: "section_mapping" and "extracted_entities".

1. section_mapping: Group block IDs into exactly these sections:
   - "ABSTRACT": paper abstract, executive summary of the work
   - "MOTIVATION_AND_BACKGROUND": research motivation, problem definition, domain challenges, why this work is needed
   - "RELATED_WORK": prior research, literature review, historical context, comparison with existing approaches
   - "METHODOLOGY": proposed algorithms, models, technical frameworks, system architecture, mathematical formulations
   - "EXPERIMENT_SETUP_AND_RESULTS": datasets, experimental setup, results, ablation studies, comparisons, runtime analysis
   - "DISCUSSION_AND_CONCLUSION": summary of contributions, future directions, limitations, broader impact
   - "TRASH": garbled tables, OCR artifacts, page headers/footers, isolated numbers/symbols, references/bibliography entries, table of contents

   RULES:
   - Every block ID from 1 to the last ID MUST appear in EXACTLY ONE section.
   - Do NOT skip any ID. Do NOT duplicate any ID.
   - Image references (e.g., "![](images/...)") and table references (e.g., "![](table/...)") MUST be kept in the section where they appear, NOT assigned to TRASH.
   - When in doubt, assign to TRASH rather than guessing.

2. extracted_entities: Extract named entities as a structured object. Each entity must have:
   - "name_in_paper": the canonical name as used by the authors
   - "aliases": an array of abbreviations or alternative names (can be empty [])
   - "semantic_definition": A concise, self-contained technical explanation (1-2 sentences), purely objective and independent of context.
   - "evidence_quote": a DIRECT COPY-PASTE from the original block text — do NOT paraphrase
   - "evidence_block_id": The integer ID of the text block where the evidence_quote was found

Categories:
   a) "research_tasks": The specific, narrow research problem or task this paper aims to solve. 
      CRITICAL: Do NOT extract broad domain names (e.g., "Image Classification" or "visual SLAM"). You MUST include the specific scenarios, constraints, or sub-fields the authors focus on (e.g., "Robust pose estimation in weakly textured and unstructured environments").
   
   b) "proposed_methods": Specific algorithms, models, loss functions, or core technical modules PROPOSED by the authors. 
      CRITICAL: Extract the actual technical names or architectures (e.g., "Efficient LoFTR front-end", "Multi-camera Joint Bundle Adjustment"). Do NOT extract long descriptive sentences as names.
   
   c) "datasets": Named public benchmarks or self-collected data arrays used to evaluate the method.
      CRITICAL: Extract proper nouns of datasets (e.g., "KITTI Odometry"). Do NOT extract software simulators/engines (e.g., "Unigine", "Unreal Engine"). Do NOT extract generic terms (e.g., "synthetic data"). If a custom dataset has no formal name, extract a concise descriptive phrase (e.g., "self-built quadcopter simulation dataset").

   d) "evaluation_metrics": Specific QUANTITATIVE metrics used to mathematically measure success.
      CRITICAL: Extract terms like "Absolute Trajectory Error", "Top-1 Accuracy", or "Runtime". Do NOT extract qualitative, subjective goals or abstract properties (e.g., "robustness", "efficiency", "performance").

   e) "baselines": External prior works, peer-reviewed methods, or existing architectures used as a comparison standard.
      CRITICAL: You MUST strictly distinguish external baselines from internal ablation studies. Do NOT extract the authors' own degraded variants or ablation models (e.g., "Ours w/o module A", "proposed method (baseline)") as baselines. 

   f) "addressed_existing_flaws": Specific technical bottlenecks or failure modes of PRIOR works that this paper explicitly aims to solve (usually found in Motivation/Introduction/Related Work).
      CRITICAL: You MUST include the "targeted_baseline" (who exactly suffers from this flaw?). Extract the specific technical root cause (e.g., "Feature-based SLAM relies on detectable keypoints which are absent in weakly textured environments"), NOT generic complaints (e.g., "Existing methods are inaccurate").

   g) "self_admitted_limitations": Flaws, boundaries, or unresolved issues that the authors EXPLICITLY concede regarding their OWN proposed method.
      CRITICAL: Look for linguistic cues in the Discussion/Conclusion (e.g., "we acknowledge", "fails to", "a major limitation is", "future work will address"). If the authors do not explicitly confess a weakness, you MUST output [] and do NOT fabricate.

CRITICAL RULES:
- Do NOT modify, paraphrase, or reword ANY original text.
- evidence_quote must be an EXACT substring from the input block specified by evidence_block_id.
- For section_mapping, use ONLY the numeric IDs.

EXAMPLE JSON OUTPUT:
{
  "section_mapping": {
    "ABSTRACT": [1, 2, 3],
    "MOTIVATION_AND_BACKGROUND": [4, 5, 6, 7],
    "RELATED_WORK": [8, 9, 10],
    "METHODOLOGY": [11, 12, 13, 14],
    "EXPERIMENT_SETUP_AND_RESULTS": [15, 16, 17],
    "DISCUSSION_AND_CONCLUSION": [18, 19],
    "TRASH": [20, 21, 22]
  },
  "extracted_entities": {
    "research_tasks": [
      {
        "name_in_paper": "Robust multi-camera visual SLAM in weakly textured and unstructured environments",
        "aliases": [],
        "semantic_definition": "The task of achieving robust pose estimation and mapping using multi-camera systems in environments lacking distinct visual features.",
        "evidence_quote": "robust and accurate pose estimation in scenarios that lack prominent geometric structures (unstructured) or exhibit sparse surface textures (weakly textured)",
        "evidence_block_id": 34
      }
    ],
    "proposed_methods": [
      {
        "name_in_paper": "Direct Multi-Camera SLAM",
        "aliases": ["proposed method"],
        "semantic_definition": "A multi-camera visual SLAM framework based on direct photometric error minimization.",
        "evidence_quote": "a multi-camera visual SLAM framework based on direct methods is proposed",
        "evidence_block_id": 30
      }
    ],
    "datasets": [
      {
        "name_in_paper": "KITTI Odometry",
        "aliases": ["KITTI"],
        "semantic_definition": "A public benchmark dataset for visual odometry in urban driving scenarios.",
        "evidence_quote": "the public KITTI Odometry dataset",
        "evidence_block_id": 103
      }
    ],
    "evaluation_metrics": [
      {
        "name_in_paper": "Absolute Trajectory Error",
        "aliases": ["ATE"],
        "semantic_definition": "The RMSE of translational differences between estimated and ground-truth trajectories.",
        "evidence_quote": "the metric used is Absolute Trajectory Error (ATE)",
        "evidence_block_id": 119
      }
    ],
    "baselines": [
      {
        "name_in_paper": "ORB-SLAM3",
        "aliases": [],
        "semantic_definition": "A feature-based visual SLAM system using ORB features.",
        "evidence_quote": "ORB-SLAM3",
        "evidence_block_id": 119
      }
    ],
    "addressed_existing_flaws": [
      {
        "name_in_paper": "feature-based methods fail in weakly textured scenes",
        "aliases": [],
        "semantic_definition": "Feature-based SLAM relies on detectable keypoints which are absent in weakly textured environments.",
        "targeted_baseline": "feature-based SLAM methods",
        "evidence_quote": "their performance strongly relies on the critical assumption that the environment contains sufficient feature points",
        "evidence_block_id": 44
      }
    ],
    "self_admitted_limitations": [
      {
        "name_in_paper": "limited real-world validation",
        "aliases": [],
        "semantic_definition": "The method has not been validated under full real-world conditions.",
        "evidence_quote": "we acknowledge that the present validation does not fully capture the full complexity of real-world conditions",
        "evidence_block_id": 190
      }
    ]
  }
}
"""


def llm_extract(client: OpenAI, indexed_blocks: list[dict],
                 model: str = "deepseek-v4-flash") -> dict | None:
    """调用 DeepSeek API 进行语义提取。"""
    blocks_text = json.dumps(indexed_blocks, ensure_ascii=False)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Analyze the following paper blocks and output the JSON:\n\n{blocks_text}"},
    ]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=32768,
            reasoning_effort="max",
            extra_body={"thinking": {"type": "enabled"}},
        )
        choice = response.choices[0]
        content = choice.message.content
        log.info("LLM usage: prompt=%s, completion=%s, finish_reason=%s",
                 response.usage.prompt_tokens if response.usage else "?",
                 response.usage.completion_tokens if response.usage else "?",
                 choice.finish_reason)
        if not content or not content.strip():
            log.warning("LLM returned empty response")
            return None
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.error("JSON parse failed: %s", e)
        return None
    except Exception as e:
        log.error("API call failed: %s", e)
        return None
