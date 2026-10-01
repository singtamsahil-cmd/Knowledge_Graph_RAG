"""Tough-question battery for GraphRAG. Saves JSON results for grading."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.rag import answer_question

QUESTIONS = [
    ("Q1 attribution", "Who approved Business Loan LN30002?", "NorthStar Bank"),
    ("Q2 status trap", "Was Home Loan LN30003 approved?", "No — under review, no approval recorded"),
    ("Q3 plant manager", "Who manages Rivergate Plant?", "Arun Menon"),
    ("Q4 supplier issue", "Which supplier had porosity in castings?", "Meridian Foundry"),
    ("Q5 name disambiguation", "Is Priya Singh the same person as Priya Nair? What does each do?",
     "No — Priya Singh owns GreenLeaf Traders; Priya Nair is Customer Service Lead"),
    ("Q6 amount in evidence", "What was the sanctioned amount of Business Loan LN30002?",
     "INR 1,200,000"),
    ("Q7 maintenance detail", "Who replaced the spindle sensor on RG-CNC-04?", "Farah Khan"),
    ("Q8 nonexistent", "Who approved Loan LN99999?", "don't know"),
    ("Q9 org account", "Which bank account does GreenLeaf Traders use?", "CA20001"),
    ("Q10 sponsor", "Who sponsors the Operations Improvement Programme?", "Meera Iyer"),
]

if __name__ == "__main__":
    out = []
    for qid, q, expected in QUESTIONS:
        try:
            r = answer_question(q, store=None)
            out.append({"id": qid, "q": q, "expected": expected,
                        "answer": r["answer"], "nfacts": len(r["facts"]),
                        "retrieval": r["retrieval"]})
        except Exception as exc:
            out.append({"id": qid, "q": q, "expected": expected,
                        "answer": f"ERROR: {exc}", "nfacts": 0, "retrieval": "error"})
        print(f"done {qid}", flush=True)
    with open("outputs/rag_battery.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("saved outputs/rag_battery.json")
