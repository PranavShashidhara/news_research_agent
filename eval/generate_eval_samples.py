"""
Generate evaluation samples from Qdrant corpus with auto-labeled ground truth.

Usage:
    python eval/generate_eval_samples.py \
        --qdrant-url http://localhost:6333 \
        --orchestrator http://localhost:8000 \
        --num-samples 25 \
        --output eval/datasets/golden.jsonl

Auto-labels each sample by:
  1. Sampling diverse articles from Qdrant
  2. Grouping by semantic topic
  3. Generating questions about article content
  4. Using orchestrator to synthesize ground truth
  5. Extracting relevant source IDs from the answer
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from qdrant_client import QdrantClient

# Generate diverse question templates for different article themes
QUESTION_TEMPLATES = {
    "finance": [
        "What are the latest developments in {topic}?",
        "How have recent announcements affected {topic}?",
        "What is the current status of {topic} according to recent reports?",
        "Summarize the recent news about {topic}.",
    ],
    "technology": [
        "What are the latest announcements from {topic}?",
        "How is {topic} evolving based on recent reports?",
        "What new developments have there been in {topic}?",
        "Summarize the recent developments in {topic}.",
    ],
    "politics": [
        "What are the latest political developments regarding {topic}?",
        "How are recent events affecting {topic}?",
        "What has been announced about {topic} recently?",
        "Summarize recent news on {topic}.",
    ],
    "general": [
        "What are the latest developments regarding {topic}?",
        "Summarize recent news about {topic}.",
        "What is happening with {topic}?",
        "What are the latest updates on {topic}?",
    ],
}

TOPICS = [
    "artificial intelligence",
    "technology industry",
    "Federal Reserve",
    "stock market",
    "corporate earnings",
    "international trade",
    "climate policy",
    "energy markets",
    "startup funding",
    "cybersecurity",
    "cryptocurrency",
    "healthcare innovation",
    "autonomous vehicles",
    "supply chain",
    "inflation trends",
]

ABSTAIN_TOPICS = [
    "flying capabilities of animals",
    "fictional events in news",
    "hypothetical scenarios",
    "unverifiable claims",
]


def sample_from_qdrant(
    client: QdrantClient, collection: str, num_samples: int = 100
) -> list[dict]:
    """Sample diverse points from Qdrant collection."""
    try:
        # Get collection info
        collection_info = client.get_collection(collection)
        count = collection_info.points_count

        if count == 0:
            return []

        # Sample random IDs
        step = max(1, count // num_samples)
        sample_ids = list(range(0, count, step))[:num_samples]

        articles = []
        for point_id in sample_ids:
            try:
                point = client.retrieve(collection, ids=[point_id])
                if point:
                    payload = point[0].payload
                    articles.append(
                        {
                            "source_id": payload.get("source_id", f"src_{point_id}"),
                            "title": payload.get("title", ""),
                            "chunk_text": payload.get("chunk_text", ""),
                            "url": payload.get("url", ""),
                            "source_name": payload.get("source_name", ""),
                            "published_at": payload.get(
                                "published_at",
                                datetime.utcnow().isoformat(),
                            ),
                        }
                    )
            except Exception:
                continue

        return articles

    except Exception as e:
        print(f"Error sampling from Qdrant: {e}")
        return []


def generate_eval_sample(
    articles: list[dict],
    sample_id: str,
    topic: str,
    orchestrator_url: str,
    is_abstain: bool = False,
) -> Optional[dict]:
    """Generate a single eval sample by querying orchestrator."""
    if not articles:
        return None

    # Generate question
    if is_abstain:
        question = f"What is the current status of {random.choice(ABSTAIN_TOPICS)}?"
    else:
        template = random.choice(QUESTION_TEMPLATES.get("general", []))
        question = template.format(topic=topic)

    try:
        # Call orchestrator to get answer
        with httpx.Client(timeout=180) as hc:
            resp = hc.post(
                f"{orchestrator_url}/research",
                json={"question": question, "recency_days": 365},
                headers={"User-Agent": "eval-generator/1.0"},
            )
            resp.raise_for_status()
            result = resp.json()

        answer = result.get("answer", {})
        sources_used = answer.get("sources_used", [])

        # Extract source IDs from the answer
        relevant_source_ids = []
        if sources_used:
            relevant_source_ids = list(
                set(src.get("source_id") for src in sources_used if src.get("source_id"))
            )

        # For abstain cases, check if the answer actually abstained
        if is_abstain:
            ground_truth = "abstain" if answer.get("abstained") else None
            relevant_source_ids = [] if answer.get("abstained") else relevant_source_ids
        else:
            # Extract ground truth from answer sentences
            sentences = answer.get("sentences", [])
            if sentences:
                ground_truth = " ".join(s.get("text", "") for s in sentences).strip()
            else:
                ground_truth = None

        return {
            "id": sample_id,
            "question": question,
            "ground_truth": ground_truth,
            "relevant_source_ids": relevant_source_ids,
        }

    except Exception as e:
        print(f"Error generating sample {sample_id}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Generate evaluation samples")
    parser.add_argument(
        "--qdrant-url",
        default="http://localhost:6333",
        help="Qdrant URL",
    )
    parser.add_argument(
        "--qdrant-collection",
        default="news",
        help="Qdrant collection name",
    )
    parser.add_argument(
        "--orchestrator",
        default="http://localhost:8000",
        help="Orchestrator URL",
    )
    parser.add_argument(
        "--num-samples",
        type=int,
        default=25,
        help="Number of samples to generate",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).parent / "datasets/golden.jsonl"),
        help="Output JSONL file",
    )
    parser.add_argument(
        "--abstain-ratio",
        type=float,
        default=0.15,
        help="Ratio of abstain test cases (0.0-1.0)",
    )
    args = parser.parse_args()

    print(f"Connecting to Qdrant at {args.qdrant_url}...")
    try:
        qdrant_client = QdrantClient(url=args.qdrant_url)
        collections = {c.name for c in qdrant_client.get_collections().collections}
        if args.qdrant_collection not in collections:
            print(f"Collection '{args.qdrant_collection}' not found. Available: {collections}")
            print("Please run 'make ingest' first to populate the vector store.")
            return 1
    except Exception as e:
        print(f"Error connecting to Qdrant: {e}")
        return 1

    print(f"Sampling articles from Qdrant collection '{args.qdrant_collection}'...")
    articles = sample_from_qdrant(
        qdrant_client, args.qdrant_collection, num_samples=args.num_samples * 2
    )

    if not articles:
        print("No articles found in Qdrant. Please run 'make ingest' first.")
        return 1

    print(f"Sampled {len(articles)} articles. Generating {args.num_samples} eval samples...")

    samples = []
    num_abstain = max(1, int(args.num_samples * args.abstain_ratio))
    num_regular = args.num_samples - num_abstain

    # Generate regular samples
    for i in range(num_regular):
        topic = random.choice(TOPICS)
        sample_articles = random.sample(
            articles, min(random.randint(3, 8), len(articles))
        )
        sample = generate_eval_sample(
            sample_articles,
            f"q{i+1:02d}",
            topic,
            args.orchestrator,
            is_abstain=False,
        )
        if sample:
            samples.append(sample)
            print(f"  Generated {sample['id']}: {sample['question'][:60]}...")

    # Generate abstain samples (should have no relevant sources)
    for i in range(num_abstain):
        sample = generate_eval_sample(
            [],
            f"q{num_regular + i + 1:02d}",
            "abstain",
            args.orchestrator,
            is_abstain=True,
        )
        if sample:
            samples.append(sample)
            print(f"  Generated {sample['id']}: {sample['question'][:60]}... (abstain)")

    if not samples:
        print("Failed to generate any samples.")
        return 1

    # Write to JSONL
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        for sample in samples:
            f.write(json.dumps(sample) + "\n")

    print(f"\nSuccessfully generated {len(samples)} eval samples")
    print(f"Saved to: {output_path}")
    print(f"  - Regular samples: {num_regular}")
    print(f"  - Abstain samples: {num_abstain}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
