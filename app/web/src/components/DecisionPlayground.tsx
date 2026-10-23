"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { apiErrorDetail } from "@/lib/api";
import {
  decisionModels, runDecision, type DecisionModel, type DecisionQuestion,
  type DecisionRequest, type DecisionResult,
} from "@/lib/decisions";
import styles from "./DecisionPlayground.module.css";

type Draft = {
  id: number; name: string; type: DecisionQuestion["type"]; instructions: string;
  options: { label: string; description: string }[];
  yes: string; no: string;
};
const emptyQuestion = (id: number): Draft => ({
  id, name: `question_${id}`, type: "noul", instructions: "", options: [
    { label: "option_a", description: "" }, { label: "option_b", description: "" },
  ], yes: "", no: "",
});
const namePattern = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/;
const percent = (value: number) => `${(value * 100).toFixed(1)}%`;

function questionFromDraft(draft: Draft): DecisionQuestion {
  const { name, type, instructions, options, yes, no } = draft;
  if (!namePattern.test(name)) throw new Error("Names must start with a letter and use up to 64 letters, numbers, dots, hyphens or underscores.");
  if (!instructions.trim()) throw new Error(`Add instructions for ${name}.`);
  if (type === "noul") {
    if (!!yes.trim() !== !!no.trim()) throw new Error(`Define both yes and no criteria for ${name}, or leave both empty.`);
    return { name, type, instructions, ...(yes.trim() ? { criteria: { true: yes, false: no } } : {}) };
  }
  if (options.some((o) => !o.description.trim())) throw new Error(`Describe every ${type === "choice" ? "option" : "level"} in ${name}.`);
  if (type === "score") {
    const criteria = options.map((o) => o.description);
    if (new Set(criteria).size !== criteria.length) throw new Error(`Use distinct levels in ${name}.`);
    return { name, type, instructions, criteria };
  }
  if (options.some((o) => !namePattern.test(o.label)) || new Set(options.map((o) => o.label)).size !== options.length) {
    throw new Error(`Use distinct valid option names in ${name}.`);
  }
  return { name, type, instructions, criteria: Object.fromEntries(options.map((o) => [o.label, o.description])) };
}

export function DecisionPlayground() {
  const [models, setModels] = useState<DecisionModel[] | null>(null);
  const [model, setModel] = useState("");
  const [availabilityError, setAvailabilityError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [input, setInput] = useState("");
  const [inputFormat, setInputFormat] = useState<"text" | "json">("text");
  const [questions, setQuestions] = useState<Draft[]>([emptyQuestion(1)]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [result, setResult] = useState<{ response: DecisionResult; request: DecisionRequest } | null>(null);
  const active = useRef<AbortController | null>(null);
  const nextId = useRef(2);
  const resultHeading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    decisionModels(controller.signal).then((available) => {
      if (controller.signal.aborted) return;
      setModels(available);
      setModel(available[0]?.id ?? "");
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setAvailabilityError(apiErrorDetail(reason));
    });
    return () => controller.abort();
  }, [refresh]);
  useEffect(() => () => active.current?.abort(), []);
  useEffect(() => { if (result) resultHeading.current?.focus(); }, [result]);

  function editQuestion(id: number, patch: Partial<Draft>) {
    setResult(null);
    setQuestions((current) => current.map((q) => q.id === id ? { ...q, ...patch } : q));
  }
  function loadExample() {
    setInput("The demo page's button label is clipped on a small screen. This is my second report.");
    setInputFormat("text");
    setQuestions([
      { ...emptyQuestion(1), name: "repeat_contact", instructions: "Does the message describe a previous report?" },
      { ...emptyQuestion(2), name: "team", type: "choice", instructions: "Which team should review this demo issue?", options: [
        { label: "design", description: "Visual appearance and layout" },
        { label: "engineering", description: "Crashes and broken functions" },
        { label: "unclear", description: "Not enough information" },
      ] },
      { ...emptyQuestion(3), name: "severity", type: "score", instructions: "Rate the impact on using the demo.", options: [
        { label: "", description: "Cosmetic only" }, { label: "", description: "Some tasks affected" },
        { label: "", description: "Demo cannot be used" },
      ] },
    ]);
    nextId.current = 4;
    setResult(null); setError(""); setNotice("Synthetic example loaded. Edit it, then run when ready.");
  }
  async function run(event: FormEvent) {
    event.preventDefault();
    if (active.current || !model) return;
    setError(""); setNotice(""); setResult(null);
    let request: DecisionRequest;
    try {
      if (!input.trim()) throw new Error("Add input to evaluate.");
      if (inputFormat === "json") JSON.parse(input);
      const named = questions.map(questionFromDraft);
      if (new Set(named.map((q) => q.name)).size !== named.length) throw new Error("Question names must be unique.");
      request = { model, input, inputFormat, questions: named };
    } catch (reason) {
      setError(reason instanceof SyntaxError ? "Input is not valid JSON. Fix it or choose Text." : apiErrorDetail(reason));
      return;
    }
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    try {
      const response = await runDecision(request, controller.signal);
      if (!controller.signal.aborted) setResult({ response, request });
    } catch (reason) {
      if (controller.signal.aborted) setNotice("Cancelled. Already accepted work may still be billed.");
      else setError(apiErrorDetail(reason));
    } finally {
      if (active.current === controller) active.current = null;
      setBusy(false);
    }
  }

  return <section className={styles.playground} aria-label="Decision playground">
    <header>
      <h2>Decision playground</h2>
      <p>Evaluate text or JSON with named yes/no, choice and score questions. Results are model judgments, not calibrated guarantees or permission to act. No explanations or automatic actions.</p>
    </header>
    {availabilityError ? <div role="alert">
      <p>{availabilityError}</p>
      <button type="button" onClick={() => { setAvailabilityError(""); setModels(null); setRefresh((v) => v + 1); }}>Reload availability</button>
    </div> : models === null ? <p role="status">Loading Decision availability...</p>
      : models.length === 0 ? <p role="status">No Decision model is available under the current model and residency policy.</p> : null}
    <form onSubmit={run}>
      <fieldset disabled={busy || !model} className={styles.editor}>
        <legend className={styles.srOnly}>Define a decision</legend>
        <div className={styles.toolbar}>
          <label>Model
            <select value={model} onChange={(e) => { setModel(e.target.value); setResult(null); }}>
              {!model && <option value="">Unavailable</option>}
              {models?.map((m) => <option key={m.id} value={m.id}>{m.displayName}</option>)}
            </select>
          </label>
          <button type="button" onClick={loadExample}>Load synthetic example</button>
        </div>
        {model && <p className={styles.note}>Processing residency: {models?.find((m) => m.id === model)?.residencies.join(", ").toUpperCase()}. Cost estimate: Unknown.</p>}
        <div className={styles.toolbar}>
          <h3>Input</h3>
          <label>Input format
            <select value={inputFormat} onChange={(e) => { setInputFormat(e.target.value === "json" ? "json" : "text"); setResult(null); }}>
              <option value="text">Text</option><option value="json">JSON (parsed)</option>
            </select>
          </label>
        </div>
        <label>Content to evaluate
          <textarea value={input} maxLength={16000} rows={5} required onChange={(e) => { setInput(e.target.value); setResult(null); }}
            aria-describedby="decision-input-help" />
        </label>
        <p id="decision-input-help" className={styles.note}>Up to 16,000 characters. Input is evaluated as data, not instructions. This playground does not save inputs or results.</p>
        <h3>Questions <span className={styles.note}>({questions.length}/8)</span></h3>
        {questions.map((q, index) => <fieldset key={q.id} className={styles.question}>
          <legend>Question {index + 1}</legend>
          <div className={styles.row}>
            <label>Name<input value={q.name} maxLength={64} required onChange={(e) => editQuestion(q.id, { name: e.target.value })} /></label>
            <label>Answer type<select value={q.type} onChange={(e) => editQuestion(q.id, {
              type: e.target.value === "choice" ? "choice" : e.target.value === "score" ? "score" : "noul",
              options: q.options.slice(0, 10),
            })}><option value="noul">Yes/no</option><option value="choice">Choice</option><option value="score">Score</option></select></label>
            <button type="button" disabled={questions.length === 1} aria-label={`Remove question ${index + 1}`}
              onClick={() => { setQuestions((current) => current.filter((item) => item.id !== q.id)); setResult(null); }}>Remove</button>
          </div>
          <label>Instructions<textarea value={q.instructions} maxLength={2000} rows={2} required
            onChange={(e) => editQuestion(q.id, { instructions: e.target.value })} /></label>
          {q.type === "noul" ? <details>
            <summary>Optional yes/no criteria</summary>
            <div className={styles.row}>
              <label>Yes means<input value={q.yes} maxLength={512} onChange={(e) => editQuestion(q.id, { yes: e.target.value })} /></label>
              <label>No means<input value={q.no} maxLength={512} onChange={(e) => editQuestion(q.id, { no: e.target.value })} /></label>
            </div>
          </details> : <>
            <p className={styles.note}>{q.type === "choice" ? "Define 2 to 12 distinct options." : "Order 2 to 10 levels from lowest to highest. Scores are fractional and zero-based."}</p>
            {q.options.map((option, optionIndex) => <div className={styles.row} key={optionIndex}>
              {q.type === "choice" ? <label>Option {optionIndex + 1} name<input value={option.label} maxLength={64} required
                onChange={(e) => editQuestion(q.id, { options: q.options.map((o, i) => i === optionIndex ? { ...o, label: e.target.value } : o) })} /></label> : <span>Level {optionIndex}</span>}
              <label>{q.type === "choice" ? `Option ${optionIndex + 1} description` : `Level ${optionIndex} label`}
                <input value={option.description} maxLength={512} required
                  onChange={(e) => editQuestion(q.id, { options: q.options.map((o, i) => i === optionIndex ? { ...o, description: e.target.value } : o) })} /></label>
              <button type="button" disabled={q.options.length === 2} aria-label={`Remove ${q.type === "choice" ? "option" : "level"} ${optionIndex + 1} from question ${index + 1}`}
                onClick={() => editQuestion(q.id, { options: q.options.filter((_, i) => i !== optionIndex) })}>Remove</button>
            </div>)}
            <button type="button" disabled={q.options.length >= (q.type === "choice" ? 12 : 10)}
              onClick={() => editQuestion(q.id, { options: [...q.options, { label: `option_${q.options.length + 1}`, description: "" }] })}>
              Add {q.type === "choice" ? "option" : "level"}
            </button>
          </>}
        </fieldset>)}
        <button type="button" disabled={questions.length >= 8} onClick={() => {
          const question = emptyQuestion(nextId.current++); setQuestions((current) => [...current, question]); setResult(null);
        }}>Add question</button>
      </fieldset>
      <div className={styles.actions}>
        <button type="submit" className={styles.primary} disabled={busy || !model}>{busy ? "Running..." : "Run decision"}</button>
        {busy && <button type="button" onClick={() => active.current?.abort()}>Cancel run</button>}
        <span className={styles.note}>One explicit call. No automatic retry.</span>
      </div>
    </form>
    {busy && <p role="status">Evaluating your questions...</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {result && <section aria-label="Decision results">
      <h3 ref={resultHeading} tabIndex={-1}>Results</h3>
      <p className={styles.note}>Reported model: {result.response.reportedModel} · {(result.response.latencyMs / 1000).toFixed(2)} s · {result.response.region} · {result.response.residency.toUpperCase()} processing</p>
      <p className={styles.note}>Tokens: {result.response.usage.inputTokens ?? "Unknown"} input / {result.response.usage.outputTokens ?? "Unknown"} output / {result.response.usage.totalTokens ?? "Unknown"} total. Cost: <strong>Unknown</strong> (no verified deployment meter).</p>
      {result.request.questions.map((q) => {
        const answer = result.response.answers[q.name];
        const probabilities = answer.type === "noul" ? { Yes: answer.noul, No: 1 - answer.noul } : answer.probabilities;
        return <article className={styles.answer} key={q.name}>
          <h4>{q.name}</h4>
          <p>{answer.type === "noul" ? `Yes probability: ${percent(answer.noul)}`
            : answer.type === "choice" ? `Selected: ${answer.choice}`
              : `Score: ${answer.score.toFixed(3)} on a 0–${q.type === "score" ? q.criteria.length - 1 : ""} scale`}</p>
          {answer.type !== "noul" && answer.confidence !== undefined && <p className={styles.note}>Reported confidence: {percent(answer.confidence)} (separate from each outcome&apos;s probability).</p>}
          <table><caption>{q.name} probability distribution</caption><thead><tr><th scope="col">Outcome</th><th scope="col">Probability</th></tr></thead>
            <tbody>{Object.entries(probabilities).map(([key, value]) => <tr key={key}>
              <th scope="row">{q.type === "score" ? `${key}: ${q.criteria[Number(key)]}` : key}</th><td>{percent(value)}</td>
            </tr>)}</tbody>
          </table>
        </article>;
      })}
    </section>}
  </section>;
}
