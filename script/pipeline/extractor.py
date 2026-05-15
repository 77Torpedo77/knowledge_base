"""Phase 2: LLM 语义提取 — 调用 DeepSeek 进行 section 分段和实体提取"""

import json
import logging
import sys

from openai import OpenAI

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a rigorous academic paper structure analyzer. You will receive a JSON array of text blocks, each with a unique "id" and "text" field. These blocks are raw lines from a paper's Markdown — headers may be missing, formatting may be garbled.

YOUR TASK: Analyze the paper blocks and output a JSON object strictly adhering to the TypeScript interfaces below.

================ SCHEMA DEFINITION ================
interface Output {
  section_mapping: {
    "ABSTRACT": number[];
    "MOTIVATION_AND_BACKGROUND": number[];
    "RELATED_WORK": number[];
    "METHODOLOGY": number[];
    "EXPERIMENT_SETUP_AND_RESULTS": number[];
    "DISCUSSION_AND_CONCLUSION": number[];
    "APPENDIX_AND_SUPPLEMENTARY": number[];
    "OTHER": number[];
    "TRASH": number[];
  };
  extracted_entities: {
    research_tasks: Entity[];
    proposed_methods: Entity[];
    datasets: DatasetEntity[];
    evaluation_metrics: Entity[];
    baselines: Entity[];
    addressed_existing_flaws: FlawEntity[];
    self_admitted_limitations: Entity[];
  }
}

interface Entity {
  name_in_paper: string;
  aliases: string[]; // output [] if none
  semantic_definition: string;
  evidence_quote: string;
  evidence_block_id: number;
}

interface FlawEntity extends Entity {
  targeted_baseline: string; // REQUIRED: who exactly suffers from this flaw?
}

interface DatasetEntity extends Entity {
  usage_role: "pre-training" | "fine-tuning" | "evaluation" | "other"; // REQUIRED: How was this dataset used in this paper?
}
==================================================

1. section_mapping: Group block IDs into exactly these sections:
   - "ABSTRACT": paper abstract, executive summary of the work
   - "MOTIVATION_AND_BACKGROUND": research motivation, problem definition, domain challenges, why this work is needed
   - "RELATED_WORK": prior research, literature review, historical context, comparison with existing approaches
   - "METHODOLOGY": proposed algorithms, models, technical frameworks, system architecture, mathematical formulations
   - "EXPERIMENT_SETUP_AND_RESULTS": datasets, experimental setup, results, ablation studies, comparisons, runtime analysis
   - "DISCUSSION_AND_CONCLUSION": summary of contributions, future directions, limitations, broader impact
   - "APPENDIX_AND_SUPPLEMENTARY": supplementary materials after the conclusion, including mathematical proofs, extra ablation studies, hyperparameter details, and extended qualitative results.
   - "OTHER": Core academic content that is valuable for paper analysis but does not strictly fit into the standard sections above.
   - "TRASH": garbled tables, OCR artifacts, page headers/footers, isolated numbers/symbols, references/bibliography entries, table of contents, and non-academic meta-information (e.g., Acknowledgements, Funding statements, Author contributions, Data Availability, Conflict of Interest).

   RULES:
   - Every block ID from 1 to the last ID MUST appear in EXACTLY ONE section.
   - Do NOT skip any ID. Do NOT duplicate any ID.
   - Image references (e.g., "![](images/...)") and table references (e.g., "![](table/...)") MUST be kept in the section where they appear, NOT assigned to TRASH.
   - When in doubt, assign to TRASH rather than guessing.
   - For "OTHER" vs "TRASH": If the text contains administrative, financial, or personal gratitude, it is "TRASH". If it contains formulas, academic concepts, or discussions about the domain that defy your explicit categories, it is "OTHER".

2. extracted_entities: Extract named entities as a structured object. Each entity must have:
   - "name_in_paper": the canonical name as used by the authors
   - "aliases": an array of abbreviations or alternative names (can be empty [])
   - "semantic_definition": A concise technical explanation (1-2 sentences). You MUST remain completely faithful to the original text. Do NOT artificially alter the authors' phrasing (e.g., if the text uses pronouns like "the method" or "it", keep them naturally).
   - "evidence_quote": A DIRECT COPY-PASTE of the FULL SENTENCE from the original block text where this entity is discussed. Do NOT just copy the entity's name. The quote MUST contain enough context to prove how the entity was used. Do NOT paraphrase.
   - "evidence_block_id": The integer ID of the text block where the evidence_quote was found.

Categories:
   a) "research_tasks": The specific, narrow research problem or task this paper aims to solve.
      CRITICAL: Do NOT extract broad domain names (e.g., "Image Classification" or "visual SLAM"). You MUST include the specific scenarios, constraints, or sub-fields the authors focus on (e.g., "Robust pose estimation in weakly textured and unstructured environments").

   b) "proposed_methods": Specific algorithms, models, loss functions, or core technical modules PROPOSED by the authors.
      CRITICAL: Extract the actual technical names or architectures (e.g., "Efficient LoFTR front-end", "Multi-camera Joint Bundle Adjustment"). Do NOT extract long descriptive sentences as names.

   c) "datasets": Named public benchmarks or self-collected data arrays used by the authors.
      CRITICAL: You MUST explicitly classify the dataset's `usage_role` (e.g., was it used for "pre-training" the foundation model, or for "evaluation" on downstream tasks?). Extract proper nouns (e.g., "KITTI Odometry"). Do NOT extract software simulators/engines (e.g., "Unigine", "Unreal Engine"). Do NOT extract generic terms (e.g., "synthetic data"). If a custom dataset has no formal name, extract a concise descriptive phrase (e.g., "self-built quadcopter simulation dataset").

   d) "evaluation_metrics": Specific QUANTITATIVE metrics used to mathematically measure success.
      CRITICAL: Extract terms like "Absolute Trajectory Error", "Top-1 Accuracy", or "Runtime". Do NOT extract qualitative, subjective goals or abstract properties (e.g., "robustness", "efficiency", "performance").
	      Do NOT use threshold expressions or mathematical notation as metric names (e.g., "delta < 1.25", "delta1", "delta3"). Instead, use the full descriptive name that disambiguates the metric (e.g., "Depth accuracy at threshold 1.25", "Percentage of inliers with relative error below 1.25").

   e) "baselines": External prior works, peer-reviewed methods, or existing architectures used as a comparison standard.
      CRITICAL: You MUST strictly distinguish external baselines from internal ablation studies. Do NOT extract the authors' own degraded variants or ablation models (e.g., "Ours w/o module A", "proposed method (baseline)") as baselines.

   f) "addressed_existing_flaws": Specific technical bottlenecks or failure modes of PRIOR works that this paper explicitly aims to solve (usually found in Motivation/Introduction/Related Work).
      CRITICAL: You MUST include the "targeted_baseline" (who exactly suffers from this flaw?). Extract the specific technical root cause (e.g., "Feature-based SLAM relies on detectable keypoints which are absent in weakly textured environments"), NOT generic complaints (e.g., "Existing methods are inaccurate").

   g) "self_admitted_limitations": Flaws, boundaries, or unresolved issues that the authors EXPLICITLY concede regarding their OWN proposed method.
      CRITICAL: Look for linguistic cues in the Discussion/Conclusion (e.g., "we acknowledge", "fails to", "a major limitation is", "future work will address"). If the authors do not explicitly confess a weakness, you MUST output [] and do NOT fabricate.

CRITICAL RULES:
- FULL SENTENCE EXTRACTION: "evidence_quote" MUST be the EXACT, contiguous FULL SENTENCE from the input block. Do NOT just extract the entity name (e.g., extracting just "KITTI Odometry" is strictly FORBIDDEN). The quote must demonstrate the context. Do NOT use ellipses "..." to shorten the quote.
- 100% FAITHFULNESS: Do NOT modify, paraphrase, or reword ANY original text for the quote. Stay entirely true to the author's original descriptions for your definitions.
- For section_mapping, use ONLY the numeric IDs.
- ARRAYS ARE DYNAMIC: Categories like baselines, datasets, and methods can have 0, 1, or MANY items depending on the paper. Extract ALL valid entities.

EXAMPLE JSON OUTPUT:
{
  "section_mapping": {
    "ABSTRACT": [1, 2, 3],
    "MOTIVATION_AND_BACKGROUND": [4, 5, 6, 7],
    "RELATED_WORK":[8, 9, 10],
    "METHODOLOGY":[11, 12, 13, 14],
    "EXPERIMENT_SETUP_AND_RESULTS":[15, 16, 17],
    "DISCUSSION_AND_CONCLUSION": [18, 19],
    "APPENDIX_AND_SUPPLEMENTARY": [20, 21],
    "OTHER": [22],
    "TRASH": [23, 24, 25]
  },
  "extracted_entities": {
    "research_tasks":[
      {
        "name_in_paper": "Robust multi-camera visual SLAM in weakly textured and unstructured environments",
        "aliases": [],
        "semantic_definition": "The task of achieving robust pose estimation and mapping using multi-camera systems in environments lacking distinct visual features.",
        "evidence_quote": "However, to fully exploit the advantages of visual SLAM in complex outdoor environments, a fundamental challenge lies in achieving robust and accurate pose estimation in scenarios that lack prominent geometric structures (unstructured) or exhibit sparse surface textures (weakly textured).",
        "evidence_block_id": 34
      }
    ],
    "proposed_methods":[
      {
        "name_in_paper": "Direct Multi-Camera SLAM",
        "aliases": ["proposed method"],
        "semantic_definition": "A multi-camera visual SLAM framework based on direct photometric error minimization.",
        "evidence_quote": "To address this issue, a multi-camera visual SLAM framework based on the direct method is proposed in this paper.",
        "evidence_block_id": 30
      }
    ],
    "datasets":[
      {
        "name_in_paper": "KITTI Odometry",
        "aliases": ["KITTI"],
        "semantic_definition": "A public benchmark dataset for visual odometry in urban driving scenarios.",
        "usage_role": "evaluation",
        "evidence_quote": "For comparison with existing approaches, the proposed algorithm is evaluated on the public KITTI Odometry dataset, which is designed for urban driving scenarios and provides stereo image sequences.",
        "evidence_block_id": 103
      },
      {
        "name_in_paper": "MCSData",
        "aliases": [],
        "semantic_definition": "A self-built simulation dataset for multi-camera visual SLAM in complex unstructured environments.",
        "usage_role": "evaluation",
        "evidence_quote": "To further assess the robustness of the proposed method in complex unstructured field environments, a dedicated simulation dataset for multi-camera visual SLAM, termed MCSData, is constructed to fill the gap in current benchmarks.",
        "evidence_block_id": 109
      }
    ],
    "evaluation_metrics":[
      {
        "name_in_paper": "Absolute Trajectory Error",
        "aliases": ["ATE"],
        "semantic_definition": "The RMSE of translational differences between estimated and ground-truth trajectories.",
        "evidence_quote": "For evaluation, loop closure is disabled for all methods (as no loops are present in the simulated data), and the metric used is Absolute Trajectory Error (ATE), which measures the Root Mean Square Error.",
        "evidence_block_id": 119
      }
    ],
    "baselines":[
      {
        "name_in_paper": "VINS-Fusion",
        "aliases": [],
        "semantic_definition": "A visual-inertial odometry system based on sliding window optimization.",
        "evidence_quote": "A comparison is conducted with four representative open-source visual SLAM systems spanning different paradigms: indirect method-based (VINS-Fusion, ORB-SLAM3), direct method-based (DSOL), and multi-camera method-based (MCVO).",
        "evidence_block_id": 119
      },
      {
        "name_in_paper": "ORB-SLAM3",
        "aliases": [],
        "semantic_definition": "A feature-based visual SLAM system using ORB features.",
        "evidence_quote": "A comparison is conducted with four representative open-source visual SLAM systems spanning different paradigms: indirect method-based (VINS-Fusion, ORB-SLAM3), direct method-based (DSOL), and multi-camera method-based (MCVO).",
        "evidence_block_id": 119
      }
    ],
    "addressed_existing_flaws":[
      {
        "name_in_paper": "feature-based methods fail in weakly textured scenes",
        "aliases": [],
        "semantic_definition": "Feature-based SLAM relies on detectable keypoints which are absent in weakly textured environments.",
        "targeted_baseline": "feature-based SLAM methods",
        "evidence_quote": "Although these approaches have achieved remarkable success in multi-camera configurations, they remain constrained by a fundamental limitation of indirect methods: their performance strongly relies on the critical assumption that the environment contains sufficient feature points.",
        "evidence_block_id": 44
      }
    ],
    "self_admitted_limitations":[
      {
        "name_in_paper": "limited real-world validation",
        "aliases": [],
        "semantic_definition": "The method has not been validated under full real-world conditions.",
        "evidence_quote": "Nevertheless, we acknowledge that the present validation does not fully capture the full complexity of real-world conditions.",
        "evidence_block_id": 190
      }
    ]
  }
}
"""


def llm_extract(client: OpenAI, indexed_blocks: list[dict],
                 model: str = "deepseek-v4-flash", *, verbose: bool = True,
                 on_progress=None) -> dict | None:
    """调用 DeepSeek API 进行语义提取。

    Args:
        verbose: 为 True 时向 stdout 输出逐字符流式进度（串行模式）。
        on_progress: callable(phase: str, chars: int) — 实时报告 LLM 流式进度。
                     phase: "thinking" | "generating"，chars: 累计字符数。
    """
    blocks_text = json.dumps(indexed_blocks, ensure_ascii=False)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Analyze the following paper blocks and output the JSON:\n\n{blocks_text}"},
    ]

    try:
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=32768,
            reasoning_effort="max",
            extra_body={"thinking": {"type": "enabled"}},
            stream=True,
        )

        content_parts = []
        reasoning_parts = []
        usage = None
        reasoning_chars = 0
        output_chars = 0

        if verbose:
            last_phase = None
            next_reasoning_report = 1000
            next_output_report = 1000

        for chunk in stream:
            if hasattr(chunk, "usage") and chunk.usage:
                usage = chunk.usage

            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                reasoning_parts.append(delta.reasoning_content)
                reasoning_chars += len(delta.reasoning_content)
                if on_progress:
                    on_progress("thinking", reasoning_chars)
                if verbose:
                    if last_phase != "thinking":
                        sys.stdout.write("LLM thinking: 0 chars")
                        sys.stdout.flush()
                        last_phase = "thinking"
                    if reasoning_chars >= next_reasoning_report:
                        sys.stdout.write(f"\rLLM thinking: {reasoning_chars} chars")
                        sys.stdout.flush()
                        next_reasoning_report += 1000
            elif delta.content:
                content_parts.append(delta.content)
                output_chars += len(delta.content)
                if on_progress:
                    on_progress("generating", output_chars)
                if verbose:
                    if last_phase != "output":
                        sys.stdout.write(f"\rLLM thinking: {reasoning_chars} chars\n")
                        sys.stdout.write("LLM generating JSON: 0 chars")
                        sys.stdout.flush()
                        last_phase = "output"
                    if output_chars >= next_output_report:
                        sys.stdout.write(f"\rLLM generating JSON: {output_chars} chars")
                        sys.stdout.flush()
                        next_output_report += 1000

        content = "".join(content_parts)
        reasoning_len = sum(len(s) for s in reasoning_parts)

        if verbose:
            if last_phase == "thinking":
                sys.stdout.write(f"\rLLM thinking: {reasoning_len} chars\n")
            elif last_phase == "output":
                sys.stdout.write(f"\rLLM generating JSON: {len(content)} chars\n")
            sys.stdout.flush()

        log.info("LLM usage: prompt=%s, completion=%s, reasoning=%d chars, output=%d chars, finish_reason=%s",
                 usage.prompt_tokens if usage else "?",
                 usage.completion_tokens if usage else "?",
                 reasoning_len, len(content),
                 chunk.choices[0].finish_reason if chunk.choices else "?")
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
