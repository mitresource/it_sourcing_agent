"""
Resume Tuner Node

Takes the FORMATTED resume (already structured JSON) as the base and
adds only the JD-relevant content that is missing.

Pipeline per candidate:
  1. GAP ANALYSIS  — LLM reads the formatted resume + JD, returns only
                     the additions needed (skills, summary sentences,
                     experience bullets). Nothing is removed.
  2. PYTHON MERGE  — Additions are merged onto the formatted resume in
                     pure Python code (no LLM involvement). The original
                     content is guaranteed to be 100% preserved.
  3. VALIDATION    — Tuned resume is checked against the formatted baseline.
  4. RE-SCORE      — LLM scores the merged resume against the JD.
  5. UPLOAD        — Structured tuned resume is uploaded to S3.
"""

import copy
import json
from typing import Dict, Any, List

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from agents.db import bench_resume_collection, tuned_resumes_collection
from agents.utils.s3_utils import upload_resume_json, tuned_s3_key, generate_presigned_url
from agents.nodes.validate_resume import validate_tuned_resume

llm = ChatOpenAI(model="gpt-4o", temperature=0)


# ── Prompt 1: Gap analysis → additions only ──────────────────────────────────

GAP_ANALYSIS_PROMPT = """
You are an expert IT ATS resume coach. Your job is to maximise a candidate's ATS match score
for a specific job description by adding the right keywords and content.

Current ATS score: {before_score}/100
Target ATS score: {target_score}/100 (you must close this gap)

Your task is ADDITIVE ONLY — do not remove or rewrite anything already in the resume.
Analyse every single requirement in the JD and identify what is MISSING or under-represented.
Be thorough and aggressive — a {score_gap}-point gap needs real content additions.

━━━ JOB DESCRIPTION ━━━
{job_details}

━━━ CANDIDATE'S FORMATTED RESUME (do NOT modify — analysis only) ━━━
{formatted_resume}

━━━ REQUIRED OUTPUT (valid JSON only — no markdown fences) ━━━
{{
  "gap_analysis": "Precise explanation of why the current score is {before_score} and what specific JD requirements are unmet.",
  "skills_to_add": {{
    "Frameworks": ["every JD-required framework missing from resume"],
    "Cloud & DevOps": ["every JD-required cloud/devops tool missing"],
    "Other Skills": ["every JD-required skill/methodology missing"]
  }},
  "summary_sentences_to_add": [
    "Targeted sentence 1 using exact JD terminology mapped to candidate's real background.",
    "Targeted sentence 2 addressing the most important JD requirement not in current summary.",
    "Targeted sentence 3 if needed — up to 3 sentences maximum."
  ],
  "experience_additions": [
    {{
      "target_entry_index": 0,
      "target_description": "employer – client (time_frame) for human reference",
      "bullets_to_add": [
        "Bullet using exact JD keyword/tool mapped to what this role actually did.",
        "Another bullet for a different JD gap applicable to this role.",
        "Up to 3 bullets per entry if there are enough genuine gaps."
      ]
    }}
  ]
}}

━━━ RULES ━━━
1. skills_to_add: Add ALL skills explicitly listed in the JD that are absent from the resume.
   Use the same category keys already in the formatted resume. No invented skills.

2. summary_sentences_to_add: Up to 3 sentences. Must use exact JD language and map to the
   candidate's real experience. Do not invent experience.

3. experience_additions: Add bullets to EVERY work entry where the JD gap is plausibly relevant.
   Up to 3 bullets per entry. Use exact JD keywords. Base bullets on what the candidate
   actually did — rephrase to surface JD-relevant aspects of their existing work.

4. If a JD requirement has ZERO basis in the candidate's background, list it in gap_analysis only.

5. You MUST produce enough additions to realistically close the {score_gap}-point gap.
   Be thorough — scan every line of the JD for keywords, tools, methodologies, and phrases
   that are not already in the resume.
"""

# ── Prompt 2: Re-score the merged resume ─────────────────────────────────────

RESCORE_PROMPT = """
You are an expert IT ATS specialist with 15+ years of recruiting experience.

This candidate was previously scored {before_score}/100 against this job.
Their resume has now been optimised with the following ATS improvements:
{improvements_summary}

Your task: score the OPTIMISED resume below against the job. Focus on:
- Keyword match: how many JD-required skills/tools now appear in the resume
- Depth of alignment: do the experience bullets reflect JD responsibilities
- Overall ATS fit improvement from the additions made

Job Details:
{job_details}

Optimised Candidate Resume:
{resume_text}

Return ONLY valid JSON:
{{
  "match_score": <number 0–100>,
  "shortlisted": <true or false>,
  "reasoning": "2–3 sentences explaining the score, explicitly mentioning improvements over the previous {before_score} score.",
  "key_strengths": ["strength 1", "strength 2"],
  "key_gaps": ["remaining gap 1", "remaining gap 2"]
}}

Score honestly but give full credit for the ATS keyword additions made.
Only shortlist if score >= 40.
"""

gap_prompt_template = ChatPromptTemplate.from_template(GAP_ANALYSIS_PROMPT)
rescore_prompt_template = ChatPromptTemplate.from_template(RESCORE_PROMPT)


def _parse_llm_json(content: str) -> dict:
    content = content.strip()
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif content.startswith("```"):
        content = content.split("```")[1].strip()
    return json.loads(content)


def _merge_additions(formatted_resume: dict, additions: dict) -> dict:
    """
    Merge JD-gap additions onto the formatted resume.
    This is pure Python — no LLM. The original content is never touched.
    Only appends to existing lists/dicts.
    """
    tuned = copy.deepcopy(formatted_resume)

    # ── Skills ──
    skills_to_add: dict = additions.get("skills_to_add", {})
    if isinstance(skills_to_add, dict):
        existing_skills = tuned.setdefault("skills", {})
        # Collect all existing skill strings (lowercase) for dedup
        all_existing = {
            s.strip().lower()
            for sl in existing_skills.values()
            for s in (sl if isinstance(sl, list) else [])
        }
        for category, new_items in skills_to_add.items():
            if not isinstance(new_items, list):
                continue
            target_list = existing_skills.setdefault(category, [])
            for skill in new_items:
                if skill.strip().lower() not in all_existing:
                    target_list.append(skill)
                    all_existing.add(skill.strip().lower())
    elif isinstance(skills_to_add, list):
        # Fallback: if LLM returned a flat list, append to Other Skills
        existing_skills = tuned.setdefault("skills", {})
        all_existing = {
            s.strip().lower()
            for sl in existing_skills.values()
            for s in (sl if isinstance(sl, list) else [])
        }
        other = existing_skills.setdefault("Other Skills", [])
        for skill in skills_to_add:
            if skill.strip().lower() not in all_existing:
                other.append(skill)
                all_existing.add(skill.strip().lower())

    # ── Professional summary ──
    sentences: List[str] = additions.get("summary_sentences_to_add", [])
    if sentences:
        summary = tuned.setdefault("profile_summary", [])
        existing_lower = {s.strip().lower() for s in summary}
        for sentence in sentences:
            if sentence.strip().lower() not in existing_lower:
                summary.append(sentence)

    # ── Experience bullets ──
    work_exp = tuned.get("work_experience", [])
    for exp_add in additions.get("experience_additions", []):
        idx = exp_add.get("target_entry_index")
        bullets: List[str] = exp_add.get("bullets_to_add", [])
        if idx is None or not isinstance(idx, int):
            continue
        if 0 <= idx < len(work_exp):
            existing_resps = work_exp[idx].setdefault("responsibilities", [])
            existing_lower = {r.strip().lower() for r in existing_resps}
            for bullet in bullets:
                if bullet.strip().lower() not in existing_lower:
                    existing_resps.append(bullet)

    return tuned


def _build_resume_text(resume: dict, meta: dict) -> str:
    """Convert the merged structured resume to plain text for re-scoring."""
    profile = resume.get("candidate_profile_details", {})
    name = profile.get("name", meta.get("name", ""))
    location = meta.get("location", "")
    email = profile.get("email", meta.get("email", ""))

    summary_bullets = "\n".join(f"  - {b}" for b in resume.get("profile_summary", []))

    flat_skills = ", ".join(
        s
        for sl in resume.get("skills", {}).values()
        for s in (sl if isinstance(sl, list) else [])
    )

    exp_lines = []
    for exp in resume.get("work_experience", []):
        employer = exp.get("employer_name", "")
        client = exp.get("client_name", "")
        company_str = f"{employer}" + (f" / Client: {client}" if client else "")
        title = exp.get("Candidate_designation", "")
        timeframe = exp.get("time_frame", "")
        exp_lines.append(f"  {title} at {company_str} ({timeframe})")
        for b in exp.get("responsibilities", []):
            exp_lines.append(f"    - {b}")

    education = resume.get("education", "")

    return f"""
Name: {name}
Location: {location}
Email: {email}

Professional Summary:
{summary_bullets}

Skills: {flat_skills}

Experience:
{chr(10).join(exp_lines)}

Education: {education}
""".strip()


async def tune_shortlisted_resumes(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node: For each shortlisted candidate in the current job:
      1. Load their formatted resume from state (output of format_shortlisted_resumes).
      2. Run gap analysis against the JD → get additions only.
      3. Merge additions onto formatted resume in Python (additive, never removes anything).
      4. Validate the merged result.
      5. Re-score against the JD.
      6. Upload to S3 and save to MongoDB.
    """
    structured_jd = state.get("structured_jd")
    current_job_id = state.get("current_job_id")

    if not structured_jd or not current_job_id:
        return {"tuned_candidates": []}

    all_shortlisted = state.get("shortlisted_candidates", [])
    current_shortlisted = [c for c in all_shortlisted if c.get("job_id") == current_job_id]

    if not current_shortlisted:
        print("   No shortlisted candidates for this job — skipping tuning.")
        return {"tuned_candidates": []}

    # Build lookup: candidate_id → formatted_resume dict
    formatted_lookup: Dict[str, dict] = {
        r["candidate_id"]: r["formatted_resume"]
        for r in state.get("formatted_resume_data", [])
        if r.get("job_id") == current_job_id and r.get("formatted_resume")
    }

    print(f"\n✏️  Tuning {len(current_shortlisted)} shortlisted resume(s) for: {structured_jd.get('job_title')}")

    job_summary = json.dumps({
        "title": structured_jd.get("job_title"),
        "company": structured_jd.get("company"),
        "skills": structured_jd.get("required_skills"),
        "location": structured_jd.get("location"),
        "type": structured_jd.get("job_type"),
        "experience": structured_jd.get("experience"),
        "responsibilities": structured_jd.get("responsibilities", [])[:10],
        "qualifications": structured_jd.get("qualifications", [])[:6],
        "description": structured_jd.get("description", "")[:2000],
    }, ensure_ascii=False)

    tuned_candidates = []

    for candidate in current_shortlisted:
        from bson import ObjectId

        candidate_id = candidate["candidate_id"]
        name = candidate["candidate_name"]
        before_score = candidate["match_score"]

        print(f"\n   [{name}] Before score: {before_score}")

        # ── Fetch metadata from DB (email, location, certifications) ──
        resume_doc = await bench_resume_collection.find_one({"_id": ObjectId(candidate_id)})
        if not resume_doc:
            print(f"   WARNING: Resume doc not found for {name} — skipping.")
            continue

        meta = {
            "name": resume_doc.get("name", name),
            "email": resume_doc.get("email", ""),
            "location": resume_doc.get("location", resume_doc.get("address", "")),
            "certifications": resume_doc.get("certifications", []),
            "education": resume_doc.get("education", []),
        }

        # ── Get formatted resume as base ──
        formatted_resume = formatted_lookup.get(candidate_id)
        if not formatted_resume:
            print(f"   WARNING: No formatted resume found for {name} — skipping tuning.")
            continue

        # ── Step 1: Gap analysis → additions ──
        target_score = min(before_score + 15, 95)
        score_gap = target_score - before_score
        try:
            gap_chain = gap_prompt_template | llm
            gap_response = await gap_chain.ainvoke({
                "job_details": job_summary,
                "formatted_resume": json.dumps(formatted_resume, ensure_ascii=False),
                "before_score": before_score,
                "target_score": target_score,
                "score_gap": score_gap,
            })
            additions = _parse_llm_json(gap_response.content)
        except Exception as e:
            print(f"   ERROR during gap analysis for {name}: {e}")
            continue

        gap_summary = additions.get("gap_analysis", "")
        skills_added = additions.get("skills_to_add", {})
        print(f"   [{name}] Gap analysis done. Skills to add: {skills_added}")

        # ── Step 2: Python merge (additive only) ──
        tuned_resume = _merge_additions(formatted_resume, additions)

        # ── Step 3: Validate ──
        is_valid, errors = validate_tuned_resume(tuned_resume, formatted_resume)
        if not is_valid:
            print(f"   [{name}] WARNING: Tuned resume validation issues:")
            for err in errors:
                print(f"      • {err}")

        # ── Build improvements list BEFORE re-score so we can pass it as context ──
        improvements_made = []
        if isinstance(skills_added, dict):
            for cat, items in skills_added.items():
                if isinstance(items, list):
                    improvements_made.extend([f"Added to {cat}: {s}" for s in items])
        elif isinstance(skills_added, list):
            improvements_made = [f"Added skill: {s}" for s in skills_added]
        sentences_added = additions.get("summary_sentences_to_add", [])
        if sentences_added:
            improvements_made.append(f"Added {len(sentences_added)} sentence(s) to professional summary")
        exp_adds = additions.get("experience_additions", [])
        if exp_adds:
            total_bullets = sum(len(e.get("bullets_to_add", [])) for e in exp_adds)
            improvements_made.append(
                f"Added {total_bullets} experience bullet(s) across {len(exp_adds)} work entry(ies)"
            )

        # ── Step 4: Re-score ──
        tuned_resume_text = _build_resume_text(tuned_resume, meta)
        after_score = before_score
        rescored = {}
        improvements_summary = "\n".join(f"- {i}" for i in improvements_made) or "General keyword optimisation"
        try:
            rescore_chain = rescore_prompt_template | llm
            rescore_response = await rescore_chain.ainvoke({
                "job_details": job_summary,
                "resume_text": tuned_resume_text,
                "before_score": before_score,
                "improvements_summary": improvements_summary,
            })
            rescored = _parse_llm_json(rescore_response.content)
            after_score = rescored.get("match_score", before_score)
        except Exception as e:
            print(f"   ERROR re-scoring {name}: {e}")

        print(f"   [{name}] After score: {after_score} (gain: +{after_score - before_score})")

        # ── Step 5: Upload to S3 ──
        tuned_s3_uri = None
        tuned_presigned_url = None
        try:
            s3_key = tuned_s3_key(current_job_id, name)
            tuned_s3_uri = upload_resume_json(tuned_resume, s3_key)
            tuned_presigned_url = generate_presigned_url(s3_key) if tuned_s3_uri else None
            print(f"   [{name}] Tuned resume uploaded → {tuned_s3_uri}")
        except Exception as e:
            print(f"   WARNING: S3 upload failed for tuned resume ({name}): {e}")

        tuned_record = {
            "job_id": current_job_id,
            "job_title": structured_jd.get("job_title", ""),
            "posted_date": structured_jd.get("posted_date", ""),
            "candidate_id": candidate_id,
            "candidate_name": name,
            "candidate_email": candidate.get("candidate_email", meta.get("email", "")),
            "before_score": before_score,
            "after_score": after_score,
            "score_gain": after_score - before_score,
            "gap_analysis": gap_summary,
            "improvements_made": improvements_made,
            "tuned_resume": {
                "summary": tuned_resume.get("profile_summary", []),
                "skills": tuned_resume.get("skills", {}),
                "experience": tuned_resume.get("work_experience", []),
                "education": meta["education"],
                "certifications": meta["certifications"],
                "location": meta["location"],
            },
            "tuned_resume_text": tuned_resume_text,
            "before_reasoning": candidate.get("reasoning", ""),
            "after_reasoning": rescored.get("reasoning", ""),
            "after_strengths": rescored.get("key_strengths", []),
            "after_gaps": rescored.get("key_gaps", []),
            "evaluated_at": state.get("timestamp"),
            "tuned_s3_uri": tuned_s3_uri,
            "tuned_presigned_url": tuned_presigned_url,
            "structured_tuned_resume": tuned_resume,
        }

        tuned_candidates.append(tuned_record)

        # ── Persist to MongoDB ──
        await tuned_resumes_collection.insert_one({
            "job_id": current_job_id,
            "job_title": structured_jd.get("job_title", ""),
            "candidate_id": candidate_id,
            "candidate_name": name,
            "candidate_email": candidate.get("candidate_email", meta.get("email", "")),
            "before_score": before_score,
            "after_score": after_score,
            "score_gain": after_score - before_score,
            "gap_analysis": gap_summary,
            "improvements_made": improvements_made,
            "tuned_resume_text": tuned_resume_text,
            "before_reasoning": candidate.get("reasoning", ""),
            "after_reasoning": rescored.get("reasoning", ""),
            "evaluated_at": state.get("timestamp"),
        })

    print(f"\n✅ Resume tuning complete for this job. {len(tuned_candidates)} resume(s) tuned.")
    return {"tuned_candidates": tuned_candidates}
