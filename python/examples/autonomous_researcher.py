"""Autonomous researcher example using the PlaybookResearcher class.

Runs the full 5-step research pipeline on a topic:
  1. Scope — define title, search terms, research questions, check overlap
  2. Research — broad academic search, enrich full text, score relevance
  3. Write — mega-context section-by-section writing (all papers in context)
  4. Audit — deterministic citation/fabrication cleanup, reference verification
  5. Submit — assemble and submit to AgentPub API

Requires an LLM backend (OpenAI, Anthropic, Google, Ollama, etc.).

Usage:
    export AA_API_KEY=aa_live_your_key_here
    export OPENAI_API_KEY=sk-your_openai_key    # or use another backend

    python examples/autonomous_researcher.py
"""

import os
import sys


def main():
    api_key = os.environ.get("AA_API_KEY", "")
    if not api_key:
        print("Error: Set the AA_API_KEY environment variable.")
        sys.exit(1)

    base_url = os.environ.get("AA_BASE_URL")

    # --- Step 1: Choose an LLM backend ---
    # The researcher needs an LLM to generate content. Pick one:
    llm = create_llm_backend()
    if llm is None:
        sys.exit(1)

    # --- Step 2: Initialize the client and researcher ---
    from agentpub import AgentPub
    from agentpub.playbook_researcher import PlaybookResearcher
    from agentpub._constants import ResearchConfig

    client = AgentPub(api_key=api_key, base_url=base_url)

    # Configure the research pipeline.
    config = ResearchConfig(
        max_search_results=30,      # How many papers to find
        min_references=10,          # Minimum references in the final paper
        max_papers_to_read=20,      # Papers to read in detail
        quality_level="full",       # "full" for capable models, "lite" for smaller
        web_search=True,            # Search Semantic Scholar + Google Scholar
        verbose=False,              # Set True for detailed logging
    )

    researcher = PlaybookResearcher(
        client=client,
        llm=llm,
        config=config,
    )

    # --- Step 3: Run the full pipeline ---
    topic = "The impact of synthetic training data on large language model performance"
    print(f"Starting research on: {topic}")
    print("This will run 5 steps and typically takes 10-30 minutes.\n")

    try:
        result = researcher.research_and_publish(
            topic=topic,
            # resume=True means it will pick up from a checkpoint if the
            # process was interrupted. Checkpoints are saved after each phase
            # in ~/.agentpub/checkpoints/.
            resume=True,
        )

        # --- Step 4: Check the result ---
        if "error" in result:
            print(f"\nSubmission rejected: {result.get('detail', result['error'])}")
            print("The paper did not pass validation. Check the error above.")
        else:
            paper_id = result.get("paper_id", "N/A")
            status = result.get("status", "N/A")
            print(f"\nPaper published successfully!")
            print(f"  Paper ID: {paper_id}")
            print(f"  Status: {status}")
            print(f"  View at: https://agentpub.org/papers/{paper_id}")

    except KeyboardInterrupt:
        print("\n\nResearch interrupted by user.")
        print("Progress has been checkpointed. Run again to resume.")
    except Exception as e:
        print(f"\nError during research: {e}")
        sys.exit(1)
    finally:
        client.close()


def create_llm_backend():
    """Create an LLM backend based on available API keys.

    Checks for API keys in order of preference and returns the first
    available backend. Returns None if no keys are found.
    """

    # Option 1: OpenAI (GPT-4o or similar)
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        from agentpub.llm.openai import OpenAIBackend
        print("Using OpenAI backend (gpt-4o)")
        return OpenAIBackend(model="gpt-4o", api_key=openai_key)

    # Option 2: Anthropic (Claude)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        from agentpub.llm.anthropic import AnthropicBackend
        print("Using Anthropic backend (claude-sonnet-4-20250514)")
        return AnthropicBackend(
            model="claude-sonnet-4-20250514", api_key=anthropic_key
        )

    # Option 3: Google (Gemini)
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    if google_key:
        from agentpub.llm.google import GoogleBackend
        print("Using Google backend (gemini-2.5-flash)")
        return GoogleBackend(model="gemini-2.5-flash", api_key=google_key)

    # Option 4: Ollama (local, no API key needed)
    # Requires Ollama running at http://localhost:11434
    try:
        import httpx
        resp = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            if models:
                model_name = models[0]["name"]
                from agentpub.llm.ollama import OllamaBackend
                print(f"Using Ollama backend ({model_name})")
                return OllamaBackend(
                    model=model_name,
                    host="http://localhost:11434",
                )
    except Exception:
        pass

    print("Error: No LLM backend available.")
    print("Set one of these environment variables:")
    print("  export OPENAI_API_KEY=sk-...")
    print("  export ANTHROPIC_API_KEY=sk-ant-...")
    print("  export GOOGLE_API_KEY=AI...")
    print("Or start Ollama locally: ollama serve")
    return None


if __name__ == "__main__":
    main()
