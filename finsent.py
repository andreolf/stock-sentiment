"""Headline sentiment: FinBERT (ProsusAI/finbert, CPU) with VADER fallback.
score_texts(texts) -> (list of floats in [-1, 1], scorer_name). FinBERT score = P(positive) - P(negative)."""
import os

_model = None
_failed = False


def _load():
    global _model, _failed
    if _model is not None or _failed:
        return _model
    if os.environ.get("NO_FINBERT"):
        _failed = True
        return None
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        torch.set_num_threads(max(1, (os.cpu_count() or 2) // 2))
        tok = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        mdl = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert").eval()
        labels = {i: l.lower() for i, l in mdl.config.id2label.items()}
        _model = (tok, mdl, labels, torch)
    except Exception as e:
        print(f"  [info] FinBERT unavailable ({type(e).__name__}: {str(e)[:120]}); using VADER")
        _failed = True
    return _model


def _vader(texts):
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    sia = SentimentIntensityAnalyzer()
    return [round(sia.polarity_scores(t or "")["compound"], 3) for t in texts], "vader"


def score_texts(texts, batch=32):
    if not texts:
        return [], "none"
    m = _load()
    if not m:
        return _vader(texts)
    tok, mdl, labels, torch = m
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), batch):
            enc = tok([t or "" for t in texts[i:i + batch]], padding=True, truncation=True, max_length=96, return_tensors="pt")
            probs = torch.softmax(mdl(**enc).logits, dim=-1).tolist()
            for p in probs:
                d = {labels[j]: v for j, v in enumerate(p)}
                out.append(round(d.get("positive", 0) - d.get("negative", 0), 3))
    return out, "finbert"
