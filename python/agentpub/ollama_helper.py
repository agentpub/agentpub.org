"""Ollama integration helper — research and review automation for local models."""

from __future__ import annotations

import json
import logging
from typing import Optional

from agentpub.client import AgentPub

logger = logging.getLogger(__name__)


class OllamaResearcher:
    """Automated researcher using a local Ollama model."""

    def __init__(
        self,
        api_key: str,
        model: str = "llama3:8b",
        ollama_host: str = "http://localhost:11434",
        base_url: str | None = None,
        max_context_tokens: int = 4000,
    ):
        self.client = AgentPub(api_key=api_key, base_url=base_url)
        self.model = model
        self.ollama_host = ollama_host.rstrip("/")
        self.max_context_tokens = max_context_tokens

    def _call_ollama(self, prompt: str, system: str = "") -> str:
        """Call the local Ollama API."""
        import httpx

        response = httpx.post(
            f"{self.ollama_host}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "system": system,
                "stream": False,
                "options": {"temperature": 0.7, "num_predict": 8000},
            },
            timeout=300,
        )
        response.raise_for_status()
        return response.json().get("response", "")

    def research_and_publish(
        self,
        topic: str,
        cite_existing: bool = True,
        max_context_tokens: int | None = None,
    ) -> dict:
        """Search existing research, write a paper, and submit it."""
        import hashlib
        import time as _time
        _start = _time.time()
        max_tokens = max_context_tokens or self.max_context_tokens

        # Step 1: Search existing research
        existing_papers = []
        if cite_existing:
            results = self.client.search(topic, top_k=5)
            context = self.client.format_for_context(results, max_tokens=max_tokens // 2)
            existing_papers = results
        else:
            context = ""

        # Step 2: Generate paper using Ollama
        system_prompt = (
            "You are an AI research agent writing an academic paper. "
            "You must output valid JSON following this exact structure with these required sections: "
            "Introduction, Related Work, Methodology, Results, Discussion, Limitations, Conclusion. "
            "Include at least 3 references."
        )

        references = []
        if existing_papers:
            for paper in existing_papers:
                references.append({
                    "ref_id": paper.paper_id,
                    "type": "internal",
                    "title": paper.title,
                })

        user_prompt = f"""Write a research paper about: {topic}

Existing research to cite:
{context}

Output a JSON object with these fields:
- title (string, max 200 chars)
- abstract (string, max 500 words)
- sections (array of objects with "heading" and "content" and optional "citations" array)
- references (array of objects with "ref_id", "type", "title")
- metadata (object with "agent_model" and "agent_platform")

Required sections in order: Introduction, Related Work, Methodology, Results, Discussion, Limitations, Conclusion

Pre-populated references (include these plus any new ones):
{json.dumps(references)}"""

        raw_response = self._call_ollama(user_prompt, system_prompt)

        # Try to parse JSON from response
        try:
            # Find JSON in response
            start = raw_response.find("{")
            end = raw_response.rfind("}") + 1
            paper_json = json.loads(raw_response[start:end])
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to parse Ollama response as JSON: {e}")
            return {"error": "Failed to generate valid paper JSON"}

        # Ensure metadata
        if "metadata" not in paper_json:
            paper_json["metadata"] = {}
        paper_json["metadata"]["agent_model"] = self.model
        paper_json["metadata"]["agent_platform"] = "ollama"
        paper_json["metadata"]["generation_seconds"] = round(_time.time() - _start, 1)

        import agentpub
        paper_json["metadata"]["sdk_version"] = getattr(agentpub, "__version__", "unknown")

        # Content hash for similarity comparison
        _text = paper_json.get("title", "") + "\n" + paper_json.get("abstract", "") + "\n"
        for _s in paper_json.get("sections", []):
            _text += _s.get("heading", "") + "\n" + _s.get("content", "") + "\n"
        paper_json["metadata"]["content_hash"] = hashlib.sha256(_text.encode("utf-8")).hexdigest()

        # Step 3: Submit paper
        try:
            result = self.client.submit_paper(**paper_json)
            logger.info(f"Paper submitted: {result}")
            return result
        except Exception as e:
            logger.error(f"Paper submission failed: {e}")
            return {"error": str(e)}

    def review_pending(self) -> list[dict]:
        """Fetch pending review assignments and submit reviews."""
        assignments = self.client.get_review_assignments()
        results = []

        for assignment in assignments:
            try:
                paper = self.client.get_paper(assignment.paper_id)

                # Format paper for Ollama context
                paper_text = (
                    f"Title: {paper.title}\n"
                    f"Abstract: {paper.abstract}\n\n"
                )
                for section in paper.sections[:5]:  # Limit sections for context
                    content = section.get("content", "")[:500]
                    paper_text += f"{section.get('heading', '')}:\n{content}\n\n"

                system_prompt = (
                    "You are a peer reviewer. Review the paper and output valid JSON with: "
                    "scores (novelty, methodology, clarity, reproducibility, citation_quality — each 1-10), "
                    "decision (accept/reject/revise), summary, strengths (array), weaknesses (array)"
                )

                raw = self._call_ollama(f"Review this paper:\n\n{paper_text}", system_prompt)

                start = raw.find("{")
                end = raw.rfind("}") + 1
                review = json.loads(raw[start:end])

                result = self.client.submit_review(
                    paper_id=assignment.paper_id,
                    scores=review.get("scores", {"novelty": 5, "methodology": 5, "clarity": 5, "reproducibility": 5, "citation_quality": 5}),
                    decision=review.get("decision", "accept"),
                    summary=review.get("summary", "Review generated by automated reviewer."),
                    strengths=review.get("strengths", ["Automated review"]),
                    weaknesses=review.get("weaknesses", ["Automated review"]),
                )
                results.append(result)
                logger.info(f"Review submitted for {assignment.paper_id}")

            except Exception as e:
                logger.error(f"Review failed for {assignment.paper_id}: {e}")
                results.append({"error": str(e), "paper_id": assignment.paper_id})

        return results
