# 🐱 ContextCat

> **AI Memory & Multi-Agent Orchestration for GitLab Duo**

**Hackathon:** GitLab AI Hackathon 2026  
**Full Project:** https://gitlab.com/chloe-kao/contextcat  
**Built by:** Chloe Kao × Claude (Anthropic) × Gemini (Google)

---

## What is ContextCat?

ContextCat is a GitLab Duo Agent Flow that eliminates AI amnesia and human relay work by acting as a **persistent memory layer and multi-agent orchestrator** for AI video production workflows.

**One mention. Six cats. Three minutes.**

## The Problem

- 🧠 **AI Amnesia** — Every new chat window = start from zero
- 🔗 **Human Relay** — Manually copying outputs between AI tools
- ⏱️ **Serial Queue** — Tasks run one by one, no parallel work

76% of workers say their AI tools lack work context. The average knowledge worker switches tools 33 times a day — wasting 44 hours per year.

## The Solution

Trigger with one `@mention` → Six specialized agents coordinate automatically → Complete video production package delivered to your GitLab Issue.

## Six Cats Architecture

| Cat | Role | Powered by |
|-----|------|-----------|
| Cat-1 | Memory Officer | Claude |
| Cat-2 | Storyboard Officer | Claude |
| Cat-3 | Visual Officer | Gemini + Imagen 3 |
| Cat-4 | Audio Director | Gemini + Veo 3 |
| Cat-4.5 | QC Inspector | Claude |
| Cat-5 | Packaging Officer | Claude |

## Full Source Code

👉 https://gitlab.com/chloe-kao/contextcat