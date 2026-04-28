"""
Build sections.json from content_list_v2_*.json files.
Processes all v2 files under MinerU/output and generates a sections.json per paper.
"""
import json, os, re, sys

BASE = r'D:\tools\MinerU\output'

# ── Keyword → normalized_type mapping (order matters: first match wins) ──
TYPE_RULES = [
    # (regex pattern, normalized_type)
    (r'\babstract\b', 'abstract'),
    (r'\bintro(duction)?\b', 'introduction'),
    (r'\breferences\b', 'references'),
    (r'\bbibliography\b', 'references'),
    (r'\brelated\s+work', 'background_or_related_work'),
    (r'\bliterature\s+review', 'background_or_related_work'),
    (r'\bbackground\b', 'background_or_related_work'),
    (r'\bproblem\s+formulation\b', 'problem_formulation'),
    (r'\bpreliminary\b', 'problem_formulation'),
    (r'\bpreliminaries\b', 'problem_formulation'),
    (r'\bnotation\b', 'problem_formulation'),
    (r'\btask\s+definition\b', 'problem_formulation'),
    (r'\b(conclusions?|concluding\s+remarks?)\b', 'conclusion'),
    (r'\bfuture\s+work\b', 'limitation_or_future_work'),
    (r'\blimitation', 'limitation_or_future_work'),
    (r'\bexperiment', 'experiment_or_evaluation'),
    (r'\bexperiential\b', 'experiment_or_evaluation'),  # OCR typo for "experimental"
    (r'\bevaluation\b', 'experiment_or_evaluation'),
    (r'\bempirical\s+study\b', 'experiment_or_evaluation'),
    (r'\bcase\s+study\b', 'experiment_or_evaluation'),
    (r'\b(simulation|dataset)\b', 'experiment_or_evaluation'),
    (r'\b(results?|analysis|ablation)\b', 'result_or_analysis'),
    (r'\bdiscussion\b', 'discussion'),
    (r'\bmethod', 'method'),
    (r'\bmethodology\b', 'method'),
    (r'\b(approach|framework|architecture)\b', 'method'),
    (r'\bmodel\b', 'method'),
    (r'\balgorithm\b', 'method'),
    (r'\bproposed\b', 'method'),
    (r'\b(overview|system\s+description|system\s+design)\b', 'method'),
    (r'\bslam\b', 'method'),
    (r'\bvins\b', 'method'),
    (r'\bvisual[-\s]inertial\b', 'method'),
    (r'\bcalibration\b', 'method'),
    (r'\bfusion\b', 'method'),
    (r'\bsmoothing\b', 'method'),
    (r'\btracking\b', 'method'),
    (r'\bmapping\b', 'method'),
    (r'\bstate\s+estimat', 'method'),
    (r'\b(filter|estimator|odometry)\b', 'method'),
    (r'\backnowledg', 'other'),
    (r'\bappendix\b', 'other'),
]

# Roman numeral patterns used for section numbering
ROMAN_PATTERN = re.compile(
    r'^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\.?\s',
    re.IGNORECASE
)
LETTER_PATTERN = re.compile(r'^([A-HJ-Z])\.\s')  # single letter (not I which could be Roman)
ARABIC_PATTERN = re.compile(r'^(\d+)\.?\s')


def extract_text(block):
    """Extract plain text from any v2 block type."""
    t = block['type']
    content = block.get('content', {})

    if t == 'paragraph':
        pieces = content.get('paragraph_content', [])
        return ' '.join(p.get('content', '') for p in pieces if p.get('type') == 'text')

    elif t == 'title':
        pieces = content.get('title_content', [])
        return ' '.join(p.get('content', '') for p in pieces if p.get('type') == 'text')

    elif t == 'list':
        items = []
        for li in content.get('list_items', []):
            item_text = ' '.join(
                p.get('content', '') for p in li.get('item_content', [])
                if p.get('type') == 'text'
            )
            if item_text:
                items.append(f'- {item_text}')
        return '\n'.join(items)

    elif t == 'equation_interline':
        eq = content.get('math_content', '')
        tag_match = re.search(r'\\tag\s*\{(.*?)\}', eq)
        tag = tag_match.group(1) if tag_match else ''
        clean_eq = re.sub(r'\\tag\s*\{.*?\}', '', eq).strip()
        if tag:
            return f'$$ {clean_eq} \\qquad ({tag}) $$'
        return f'$$ {clean_eq} $$'

    elif t == 'image':
        caption_parts = []
        for cap_block in content.get('image_caption', []):
            cap_text = extract_text({'type': cap_block['type'], 'content': cap_block})
            if cap_text:
                caption_parts.append(cap_text)
        return '[Figure] ' + ' '.join(caption_parts) if caption_parts else '[Figure]'

    return ''


def get_title_text(block):
    """Get the title text from a title block."""
    pieces = block['content'].get('title_content', [])
    return ' '.join(p.get('content', '') for p in pieces if p.get('type') == 'text').strip()


def infer_level(title_text):
    """Infer section level from numbering pattern."""
    # Check letter pattern first (A., B., etc.) — single letters like C, D, L, M, V, X
    # are also valid Roman numerals, but are far more likely to be sub-section letters.
    # Letter 'I' is excluded from LETTER_PATTERN to avoid conflict with Roman I.
    if LETTER_PATTERN.match(title_text):
        return 2
    if ROMAN_PATTERN.match(title_text):
        return 1
    if ARABIC_PATTERN.match(title_text):
        return 3
    # Unnumbered sections at top level
    upper_ratio = sum(1 for c in title_text if c.isupper()) / max(len(title_text), 1)
    if upper_ratio > 0.5:
        return 1
    return 1


def classify_type(title_text):
    """Classify normalized_type from title text using keyword rules."""
    title_lower = title_text.lower()
    for pattern, ntype in TYPE_RULES:
        if re.search(pattern, title_lower):
            # Determine confidence
            # High: very specific match
            high_patterns = [
                r'^i+\.\s*introduction', r'^i+\.\s*related\s+work',
                r'^v+\.\s*conclu', r'^references',
                r'^abstract'
            ]
            confidence = 'high' if any(re.search(p, title_lower) for p in high_patterns) else 'medium'
            return ntype, confidence
    return 'other', 'low'


def strip_numbering(title_text):
    """Remove section numbering prefix (I., A., 1., etc.) for cleaner raw_title."""
    t = title_text.strip()
    t = ROMAN_PATTERN.sub('', t, count=1).strip()
    t = LETTER_PATTERN.sub('', t, count=1).strip()
    t = ARABIC_PATTERN.sub('', t, count=1).strip()
    return t


def is_section_title(title_text):
    """Check if a title looks like a section heading (vs. paper title)."""
    t = title_text.strip()
    # Has numbering
    if ROMAN_PATTERN.match(t) or LETTER_PATTERN.match(t) or ARABIC_PATTERN.match(t):
        return True
    # All-caps and not the paper title (which is long)
    if t.isupper() and len(t) < 80:
        return True
    return False


def process_paper(v2_path):
    """Process one v2 JSON file into sections dict."""
    with open(v2_path, 'r', encoding='utf-8') as f:
        pages = json.load(f)

    # Extract paper_id from filename
    fname = os.path.basename(v2_path)
    paper_id = fname.replace('content_list_v2_', '').replace('.json', '')

    # Flatten all blocks with page_idx
    all_blocks = []
    for page_idx, page in enumerate(pages):
        for block in page:
            all_blocks.append((page_idx, block))

    # Find paper title: first title block on page 0 (not a section heading)
    paper_title = paper_id  # fallback
    pre_section_blocks = []  # blocks before first section title
    first_section_idx = None

    for i, (pg_idx, block) in enumerate(all_blocks):
        if block['type'] == 'title':
            title_text = get_title_text(block)
            if is_section_title(title_text):
                first_section_idx = i
                # Everything before this (on page 0) might contain paper title
                for j in range(i):
                    if all_blocks[j][1]['type'] == 'title':
                        paper_title = get_title_text(all_blocks[j][1])
                break
            elif pg_idx == 0:
                paper_title = title_text

    if first_section_idx is None:
        first_section_idx = len(all_blocks)

    # Collect sections: walk through blocks, split by title blocks
    sections = []
    current_title_idx = None
    current_title_text = ''
    current_start_page = 0
    current_text_blocks = []

    # Add an implicit "pre-section" for content before the first heading (like abstract)
    pre_text_parts = []
    for i in range(first_section_idx):
        pg, block = all_blocks[i]
        txt = extract_text(block)
        if txt.strip():
            pre_text_parts.append(txt)
    pre_text = '\n\n'.join(pre_text_parts)

    # Detect abstract from pre-section text
    abstract_text = ''
    other_pre_text = pre_text
    abs_match = re.search(r'Abstract[—\-–]\s*(.+?)(?=\bI\.\s+INTRO|\bI\.\s+Intro|\Z)', pre_text, re.DOTALL)
    if abs_match:
        abstract_text = abs_match.group(1).strip()
        other_pre_text = pre_text.replace(abs_match.group(0), '').strip()

    if abstract_text:
        sections.append({
            'section_id': f'{paper_id}-S01',
            'order': 1,
            'level': 1,
            'raw_title': 'Abstract',
            'normalized_type': 'abstract',
            'confidence': 'high',
            'page_start': 0,
            'page_end': 0,
            'text': abstract_text
        })

    # Process title-bound sections
    order_counter = len(sections) + 1
    current_parent_type = 'other'
    i = first_section_idx
    while i < len(all_blocks):
        pg_idx, block = all_blocks[i]

        if block['type'] == 'title':
            title_text = get_title_text(block)
            if is_section_title(title_text):
                # Save previous section if any (allow empty text for container headings)
                if current_title_text:
                    sec_text = '\n\n'.join(current_text_blocks).strip()
                    ntype, confidence = classify_type(current_title_text)
                    level = infer_level(current_title_text)

                    # Sub-section inherits parent type if classification is uncertain
                    if level > 1 and ntype == 'other':
                        ntype = current_parent_type
                        confidence = 'medium'

                    sections.append({
                        'section_id': f'{paper_id}-S{order_counter:02d}',
                        'order': order_counter,
                        'level': level,
                        'raw_title': strip_numbering(current_title_text),
                        'normalized_type': ntype,
                        'confidence': confidence,
                        'page_start': current_start_page,
                        'page_end': pg_idx,
                        'text': sec_text
                    })

                    # Track parent type for sub-sections
                    if level == 1:
                        current_parent_type = ntype

                    order_counter += 1

                current_title_text = title_text
                current_start_page = pg_idx
                current_text_blocks = []
                i += 1
                continue

        # Collect text for current section
        txt = extract_text(block)
        if txt.strip():
            current_text_blocks.append(txt)
        i += 1

    # Don't forget the last section
    if current_title_text:
        sec_text = '\n\n'.join(current_text_blocks).strip()
        last_page = all_blocks[-1][0]
        ntype, confidence = classify_type(current_title_text)
        level = infer_level(current_title_text)

        if level > 1 and ntype == 'other':
            ntype = current_parent_type
            confidence = 'medium'

        sections.append({
            'section_id': f'{paper_id}-S{order_counter:02d}',
            'order': order_counter,
            'level': level,
            'raw_title': strip_numbering(current_title_text),
            'normalized_type': ntype,
            'confidence': confidence,
            'page_start': current_start_page,
            'page_end': last_page,
            'text': sec_text
        })
        order_counter += 1

    result = {
        'paper_id': paper_id,
        'parser': 'mineru',
        'title': paper_title,
        'sections': sections
    }
    return result


def main():
    v2_files = []
    for root, dirs, files in os.walk(BASE):
        for f in files:
            if f.startswith('content_list_v2_') and f.endswith('.json'):
                v2_files.append(os.path.join(root, f))
    v2_files.sort()

    for fp in v2_files:
        paper_dir = os.path.dirname(fp)
        print(f'Processing: {os.path.basename(fp)}')
        result = process_paper(fp)

        out_path = os.path.join(paper_dir, 'sections.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        n_secs = len(result['sections'])
        types = [s['normalized_type'] for s in result['sections']]
        print(f'  -> {n_secs} sections: {types}')
        print(f'  -> wrote {out_path}')

if __name__ == '__main__':
    main()
