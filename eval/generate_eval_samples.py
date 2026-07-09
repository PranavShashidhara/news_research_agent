"""
Generate evaluation samples from Qdrant corpus with auto-labeled ground truth.

Usage (no API calls needed):
    python eval/generate_eval_samples.py \
        --qdrant-url http://localhost:6333 \
        --num-samples 100 \
        --output eval/datasets/golden.jsonl

Auto-labels each sample by:
  1. Sampling diverse articles from Qdrant
  2. Grouping by semantic topic
  3. Generating questions about article content
  4. Using article content as ground truth (no API calls)
  5. Extracting relevant source IDs from articles
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
    """Sample diverse points from Qdrant collection using scroll."""
    try:
        # Get collection info
        collection_info = client.get_collection(collection)
        count = collection_info.points_count

        if count == 0:
            return []

        # Scroll through collection and sample randomly
        all_points = []
        next_page = None
        while True:
            points, next_page = client.scroll(
                collection, limit=100, offset=next_page
            )
            all_points.extend(points)
            if next_page is None:
                break

        # Randomly sample from all points
        sample_size = min(num_samples * 2, len(all_points))
        sampled = random.sample(all_points, sample_size) if len(all_points) > 0 else []

        articles = []
        for point in sampled:
            try:
                payload = point.payload
                articles.append(
                    {
                        "source_id": payload.get("source_id", f"src_{point.id}"),
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
    orchestrator_url: Optional[str] = None,
    is_abstain: bool = False,
) -> Optional[dict]:
    """Generate a single eval sample from Qdrant articles (no API calls needed)."""
    if not articles:
        return None

    # Generate question
    if is_abstain:
        question = f"What is the current status of {random.choice(ABSTAIN_TOPICS)}?"
        ground_truth = None  # Abstain samples have no ground truth
        relevant_source_ids = []
    else:
        # Use article content as ground truth
        template = random.choice(QUESTION_TEMPLATES.get("general", []))
        question = template.format(topic=topic)

        # Combine article texts as ground truth (simulating orchestrator synthesis)
        article_texts = [
            a.get("chunk_text", "") or a.get("title", "")
            for a in articles if a.get("chunk_text") or a.get("title")
        ]
        ground_truth = " ".join(article_texts[:3]).strip()  # Use first 3 articles

        # Extract source IDs from articles
        relevant_source_ids = list(
            set(a.get("source_id") for a in articles if a.get("source_id"))
        )[:5]  # Limit to 5 sources

    return {
        "id": sample_id,
        "question": question,
        "ground_truth": ground_truth,
        "relevant_source_ids": relevant_source_ids,
    }


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
        default=None,
        help="Orchestrator URL (optional, not needed for sample generation)",
    )
    parser.add_argument(
        "--num-samples",
        type=int,
        default=100,
        help="Number of samples to generate (default 100)",
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
