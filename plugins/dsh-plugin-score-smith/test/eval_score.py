"""批量识谱评测：跑 score_worker，把结果与 ground truth 对照。

用法（曲谱 venv）：
  python test/eval_score.py --cases "D:\\dsh 大肥鱼\\tmp\\eval\\cases" --tag base

每个 case 目录里要有 mix.wav、answer.json、以及各分轨 <stem>.wav。
评测口径：
  * 音准/音符：mir_eval.transcription（onset 容差 50 ms、pitch 容差 50 cents，
    不看时值），给出 P / R / F1；
  * onset-only F1：只比对起音时间，用来分离「节奏对但音高错」和「节奏也错」；
  * 八度错：最近音高差是 12 的整数倍且非 0；
  * 反复记号：直接调 parts._section_marks 看是否抓到 A A B 结构。
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
PY_DIR = PLUGIN / "py"
sys.path.insert(0, str(PY_DIR))

import mir_eval.transcription as mt  # noqa: E402


def note_arrays(notes, origin, sec_per_ql):
    """score.json 音符 -> (intervals, midi) 秒制。"""
    starts, ends, pitches = [], [], []
    for note in notes:
        if "offsetQL" in note:
            s = origin + float(note["offsetQL"]) * sec_per_ql
            d = max(1e-3, float(note["durationQL"]) * sec_per_ql)
            e = s + d
        else:
            s = float(note["start"])
            e = s + max(1e-3, float(note["dur"]))
        starts.append(s)
        ends.append(e)
        pitches.append(0.0 if note.get("pitch") is None else float(note["pitch"]))
    if not starts:
        return np.zeros((0, 2)), np.zeros(0)
    return np.column_stack([starts, ends]), np.asarray(pitches)


def match_onsets(ref_iv, ref_p, est_iv, est_p, tol=0.06):
    """按起音时间贪心配对，返回逐对 (ref_idx, est_idx)。"""
    pairs = []
    used = set()
    order = np.argsort(ref_iv[:, 0]) if len(ref_iv) else []
    for i in order:
        t = ref_iv[i, 0]
        best = None
        best_dt = tol
        for j in range(len(est_iv)):
            if j in used:
                continue
            dt = abs(est_iv[j, 0] - t)
            if dt <= best_dt:
                best, best_dt = j, dt
        if best is not None:
            used.add(best)
            pairs.append((int(i), int(best)))
    return pairs


def onset_scores(ref_iv, est_iv, tol=0.05):
    if not len(ref_iv) or not len(est_iv):
        return 0.0, 0.0, 0.0
    pairs = match_onsets(ref_iv, np.zeros(len(ref_iv)), est_iv,
                         np.zeros(len(est_iv)), tol=tol)
    tp = len(pairs)
    precision = tp / len(est_iv)
    recall = tp / len(ref_iv)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def score_stem(ref_notes, est_notes, origin, sec_per_ql, drums=False):
    ref_iv, ref_p = note_arrays(ref_notes, 0.0, sec_per_ql)
    est_iv, est_p = note_arrays(est_notes, origin, sec_per_ql)
    out = {
        "ref_count": int(len(ref_p)),
        "est_count": int(len(est_p)),
        "precision": 0.0, "recall": 0.0, "f1": 0.0,
        "onset_f1": 0.0,
        "octave_errors": 0, "pitch_errors": 0, "unmatched_ref": 0,
        "unmatched_est": 0,
    }
    if not len(ref_p) or not len(est_p):
        out["unmatched_ref"] = int(len(ref_p))
        out["unmatched_est"] = int(len(est_p))
        return out
    if drums:
        p, r, f1 = onset_scores(ref_iv, est_iv)
        out.update(precision=round(p, 4), recall=round(r, 4), f1=round(f1, 4),
                   onset_f1=round(f1, 4))
        pairs = match_onsets(ref_iv, ref_p, est_iv, est_p, tol=0.06)
        out["unmatched_ref"] = int(len(ref_p) - len(pairs))
        out["unmatched_est"] = int(len(est_p) - len(pairs))
        return out
    ref_hz = 440.0 * 2 ** ((ref_p - 69) / 12.0)
    est_hz = 440.0 * 2 ** ((est_p - 69) / 12.0)
    p, r, f1, _ = mt.precision_recall_f1_overlap(
        ref_iv, ref_hz, est_iv, est_hz,
        onset_tolerance=0.05, pitch_tolerance=50.0, offset_ratio=None)
    out.update(precision=round(float(p), 4), recall=round(float(r), 4),
               f1=round(float(f1), 4))
    op, orc, of1 = onset_scores(ref_iv, est_iv)
    out["onset_f1"] = round(float(of1), 4)
    pairs = match_onsets(ref_iv, ref_p, est_iv, est_p, tol=0.06)
    matched_ref = set()
    matched_est = set()
    for i, j in pairs:
        matched_ref.add(i)
        matched_est.add(j)
        semis = float(est_p[j] - ref_p[i])
        r12 = semis - 12 * round(semis / 12.0)
        if abs(semis) < 0.6:
            continue
        if abs(r12) < 0.6 and abs(semis) >= 10:
            out["octave_errors"] += 1
        else:
            out["pitch_errors"] += 1
    out["unmatched_ref"] = int(len(ref_p) - len(matched_ref))
    out["unmatched_est"] = int(len(est_p) - len(matched_est))
    return out


def articulation_report(ref_all, est_notes, origin, sec_per_ql):
    """倚音（grace）和连音（slur）检出对照。"""
    def est_t(n):
        return origin + float(n["offsetQL"]) * sec_per_ql
    ref_grace = [n for n in ref_all if n.get("grace")]
    ref_slur = [n for n in ref_all if n.get("slur_in")]
    est_grace = [n for n in est_notes if n.get("grace")]
    est_slur = [n for n in est_notes if n.get("slur_in")]
    grace_hit = 0
    for ref in ref_grace:
        for e in est_grace:
            if abs(est_t(e) - float(ref["start"])) <= 0.12 and int(e["pitch"]) == int(ref["pitch"]):
                grace_hit += 1
                break
    slur_hit = 0
    for ref in ref_slur:
        for e in est_slur:
            if abs(est_t(e) - float(ref["start"])) <= 0.10 and int(e["pitch"]) == int(ref["pitch"]):
                slur_hit += 1
                break
    return {"grace_ref": len(ref_grace), "grace_est": len(est_grace), "grace_hit": grace_hit,
            "slur_ref": len(ref_slur), "slur_est": len(est_slur), "slur_hit": slur_hit}


def run_worker(case_dir, work_dir, tag):
    cmd = [sys.executable, str(PY_DIR / "score_worker.py"), "transcribe",
           "--input", str(case_dir / "mix.wav"),
           "--work-dir", str(work_dir),
           "--stems-dir", str(case_dir),
           "--title", f"{case_dir.name}-{tag}",
           "--no-preview"]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(cmd, cwd=str(PLUGIN), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=900)
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-2000:])
    return cmd


def structure_report(score, stem_id):
    """反复记号/连音线结构检查。"""
    import quantize as qz
    import parts
    meta = score["meta"]
    meter = int(meta.get("meter", 4))
    for track in score["tracks"]:
        if track["id"] != stem_id:
            continue
        notes = track["notes"]
        if not notes:
            return {"ok": False, "reason": "no notes"}
        total_ql = max(n["offsetQL"] + n["durationQL"] for n in notes)
        measures = max(1, int((total_ql + meter - 1) // meter))
        by_measure = qz.group_by_measure(notes, meter)
        try:
            marks = parts._section_marks(by_measure, measures, meter)
        except TypeError:
            marks = parts._section_marks(by_measure, measures, meter, 4, 0.90)
        return {"marks": marks, "measures": measures}
    return {"ok": False, "reason": f"no track {stem_id}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=r"D:\dsh 大肥鱼\tmp\eval\cases")
    ap.add_argument("--work", default=r"D:\dsh 大肥鱼\tmp\eval\runs")
    ap.add_argument("--tag", default="base")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip-run", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cases_dir = Path(args.cases)
    work_root = Path(args.work)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    rows = []
    summary = {}
    for case_dir in sorted(p for p in cases_dir.iterdir() if p.is_dir()):
        if only and case_dir.name not in only:
            continue
        answer = json.loads((case_dir / "answer.json").read_text(encoding="utf-8"))
        work_dir = work_root / f"{case_dir.name}-{args.tag}"
        if not args.skip_run:
            if work_dir.exists():
                import shutil
                shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True, exist_ok=True)
            print(f"[run] {case_dir.name} -> {work_dir}")
            run_worker(case_dir, work_dir, args.tag)
        score_path = work_dir / "score.json"
        if not score_path.exists():
            print(f"[skip] {case_dir.name}: no score.json")
            continue
        score = json.loads(score_path.read_text(encoding="utf-8"))
        meta = score["meta"]
        tempo = float(meta["tempo"])
        sec_per_ql = 60.0 / tempo
        origin = float(meta.get("originSec", 0.0))
        tracks = {t["id"]: t for t in score["tracks"]}
        case_summary = {
            "tempo": tempo, "meter": meta.get("meter"),
            "key": f'{meta.get("keyTonic")}:{meta.get("keyMode")}',
            "grid": meta.get("grid"), "stems": {},
        }
        for stem, info in answer["stems"].items():
            track = tracks.get(stem)
            key = f"{case_dir.name}:{stem}"
            if track is None:
                print(f"{key:26s} MISSING TRACK")
                case_summary["stems"][stem] = {"missing": True}
                continue
            est = track["notes"]
            ref_notes = info["notes"]
            main_ref = [n for n in ref_notes if not n.get("grace")]
            drums = track.get("instrument") == "drums" or stem == "drums"
            est_main = [n for n in est if not n.get("grace")] if not drums else est
            res = score_stem(main_ref, est_main, origin, sec_per_ql, drums=drums)
            res["artic"] = articulation_report(ref_notes, est, origin, sec_per_ql)
            case_summary["stems"][stem] = res
            rows.append((case_dir.name, stem, res))
            artic = res["artic"]
            print(f"{key:26s} ref={res['ref_count']:3d} est={res['est_count']:3d} "
                  f"P={res['precision']:.3f} R={res['recall']:.3f} F1={res['f1']:.3f} "
                  f"onset={res['onset_f1']:.3f} oct={res['octave_errors']:2d} "
                  f"pitch_err={res['pitch_errors']:2d} extra={res['unmatched_est']:3d} "
                  f"miss={res['unmatched_ref']:3d} grace={artic['grace_hit']}/{artic['grace_ref']} "
                  f"slur={artic['slur_hit']}/{artic['slur_ref']}")
        if "voice_legato" in case_dir.name:
            st = structure_report(score, "vocals")
            case_summary["structure"] = st
            marks = st.get("marks") if isinstance(st, dict) else None
            print(f"{case_dir.name:26s} structure: {json.dumps(marks, ensure_ascii=False, default=str)}")
        # 答案里的结构对照
        for stem, info in answer["stems"].items():
            if info.get("repeat"):
                print(f"{case_dir.name:26s} expected repeat: {json.dumps(info['repeat'], ensure_ascii=False)}")
        summary[case_dir.name] = case_summary

    out_path = Path(args.out) if args.out else work_root / f"summary-{args.tag}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\nsummary -> {out_path}")
    if rows:
        f1s = [r[2]["f1"] for r in rows if r[1] != "drums"]
        if f1s:
            print(f"mean F1 (non-drums) = {sum(f1s) / len(f1s):.4f}")


if __name__ == "__main__":
    main()
