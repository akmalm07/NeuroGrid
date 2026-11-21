---
name: NeuroGrid
description: A technical editorial website for distributed inference.
colors:
  violet: "#a5a1ff"
  violet-hover: "#c3c0ff"
  action-ink: "#0d0d21"
  green: "#64e8b4"
  status-idle: "#8993aa"
  error: "#ffb8b6"
  midnight: "#080b14"
  surface: "#101523"
  surface-raised: "#141b2c"
  rule: "#252d41"
  ink: "#f1f3fb"
  secondary: "#a3aec5"
typography:
  display:
    fontFamily: 'Inter, "Segoe UI", system-ui, sans-serif'
    fontSize: "103px"
    fontWeight: 690
    lineHeight: 0.99
    letterSpacing: "-0.075em"
  headline:
    fontFamily: 'Inter, "Segoe UI", system-ui, sans-serif'
    fontSize: "68px"
    fontWeight: 690
    lineHeight: 1.05
    letterSpacing: "-0.069em"
  title:
    fontFamily: 'Inter, "Segoe UI", system-ui, sans-serif'
    fontSize: "35px"
    fontWeight: 670
    lineHeight: 1.2
    letterSpacing: "-0.05em"
  body:
    fontFamily: 'Inter, "Segoe UI", system-ui, sans-serif'
    fontSize: "16px"
    lineHeight: 1.6
  label:
    fontFamily: "ui-monospace, monospace"
    fontSize: "11px"
    lineHeight: 1.4
    letterSpacing: "0.07em"
rounded:
  field: "7px"
  control: "8px"
  panel: "11px"
spacing:
  panel-gap: "14px"
  side-panel-padding: "18px"
  registry-padding: "22px"
components:
  button-primary:
    backgroundColor: "{colors.violet}"
    textColor: "{colors.action-ink}"
    rounded: "{rounded.control}"
    padding: "9px 15px"
  button-primary-hover:
    backgroundColor: "{colors.violet-hover}"
  button-secondary:
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "9px 15px"
  suggestion-chip:
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "9px 15px"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
    padding: "{spacing.side-panel-padding}"
  partition-card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
    padding: "21px"
  prompt-field:
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    padding: "10px 13px"
---

# NeuroGrid design system

## Overview

The public website pairs large editorial headlines with a navy graphite palette. Three cobalt-lit chips illustrate the model partitions on the homepage. Chat, Network, Ledger, and Downloads carry the same typography and palette into compact operational views.

**Key characteristics**

- Equal visual prominence for trying chat and contributing compute.
- Thin rules and compact panels for technical data.
- Text labels for availability, progress, and verification.

## Colors

Violet identifies actions, active navigation, focus, and line icons. Cool off-white carries headings and important values. Muted slate carries explanations and secondary data. Midnight, surface, raised surface, and thin rules establish hierarchy.

Mint green marks coordinator-reported availability and positive ledger states. Idle, paused, and offline indicators use muted slate. Pale coral identifies form and verification errors.

## Typography

Use the Inter stack for display, headings, and UI copy. Heading tokens show the largest CSS sizes. The stylesheet scales display, page, and section headings fluidly, with a separate mobile range for the hero. Use monospace for layer labels, node identifiers, hashes, commands, and keys. Use tabular figures for prominent metrics and balances.

## Layout

The shared content container tops out at 1,200 px. Side gutters step from 48 px to 28 px at 900 px and to 18 px at 650 px. At 650 px and below, navigation moves to a second row, the homepage chip artwork drops behind the lower part of the hero, and the two hero actions stay paired. Home metrics become a two-by-two grid. Partition, workflow, download, and chat columns stack. Network metrics stay in three compact columns, and the node registry scrolls horizontally within its panel.

## Elevation & Depth

Surfaces use tonal layering and one-pixel rules. The homepage artwork fades into the page ground, and the small green status glow emphasizes active availability.

## Shapes

Controls and fields have gently rounded corners. Panels have a slightly larger radius. Status dots are circular. Commands and hashes wrap within their containers on narrow screens.

## Components

- Fill both homepage actions with violet and give them equal grid widths. Use bordered controls for secondary actions and chat suggestions.
- Use a violet underline and brighter text for the current navigation item. Give keyboard focus a two-pixel violet outline with a four-pixel offset. Respect reduced-motion preferences.
- Pair each partition's short name with its layer range and a text status. Highlight the currently executing partition with a violet border. Keep the same stage order in the chat pipeline.
- Use a dark inset field for the chat composer. Keep errors near the action they affect. Group blocks and their expandable details in a ruled ledger panel.

## Do's and Don'ts

- Do pair each availability indicator with a visible or accessible status label.
- Do keep the ledger and node registry compact and preserve the readability of identifiers at mobile sizes.
