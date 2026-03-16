# ContextCat — Agent Guide

## Project Overview
ContextCat is a GitLab Duo Agent Flow that acts as a persistent memory layer
and multi-agent orchestrator for AI video production workflows.

## The Six Cats
- **Cat-1** (Memory Officer / Claude): Reads GitLab Issue memory, checks context sufficiency
- **Cat-2** (Storyboard Officer / Claude): Calculates clips, generates structured JSON
- **Cat-3** (Visual Officer / Gemini + Imagen 3): Builds Story Bible, generates reference images
- **Cat-4** (Audio Director / Gemini + Veo 3): Generates video + voiceover + SFX + music
- **Cat-4.5** (QC Inspector / Claude): Checks first/last frames for AI artifacts
- **Cat-5** (Packaging Officer / Claude): Collects outputs, updates Issue, notifies user

## Memory Store
All project context lives in GitLab Issues as structured markdown.
Agents read and write to Issues — no external database required.

## Trigger
Mention `@ai-contextcat-part-1-chloe-kao` in any Issue comment to start the flow.

## Key Design Decisions
- JSON prompts split into `visual` and `audio` blocks to prevent cross-contamination
- Story Bible built by Gemini before image generation to ensure visual consistency
- Human Gate 1 placed after reference images (free) before Veo 3 (costly)
- Cloud Run acts as enterprise security boundary — GitLab Flow never calls external APIs directly