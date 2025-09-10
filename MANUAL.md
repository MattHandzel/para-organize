# `para-organize.nvim` Manual

This document provides a detailed guide to the features and workflows of `para-organize.nvim`.

## Core Concepts

### The Index

The plugin's speed and intelligence come from a JSON-based index of your notes. This index stores file paths and parsed frontmatter. To keep it up-to-date, run `:PARAOrganize reindex` whenever you add or modify notes outside of the plugin's workflow. The index is stored in your Neovim data directory (e.g., `~/.local/share/nvim/para_org_index.json`).

### The Organizer UI

The main interface is a two-pane layout that appears after you select a note to process.

- **Left Pane (Capture Note)**: This is a fully editable buffer containing your note. You can make changes, fix typos, or add metadata before deciding where to file it.
- **Right Pane (Action Pane)**: This pane is for taking action. It starts by showing a list of suggestions, but can change to show a list of notes in a folder or a note you want to merge into.

## Workflows

There are two primary workflows for processing a note.

### 1. Quick Organize

This is the fastest way to file a note when the suggestions are accurate.

1.  Run `:PARAOrganize start`.
2.  Select a note from the Telescope picker.
3.  In the right-hand **Suggestions** list, navigate to a destination (e.g., `[P] My-Project`).
4.  Press `<CR>`.
5.  The original note is copied to the destination folder and the capture is archived. The UI closes.

### 2. Merge Flow

Use this workflow when you want to consolidate a new capture into an existing note.

1.  Run `:PARAOrganize start` and select a capture note.
2.  In the right-hand **Suggestions** list, press `/` to open the folder finder.
3.  Use Telescope to find the destination folder (e.g., `Resources/Programming`). Press `<CR>`.
4.  The right pane now shows a list of all notes inside that folder.
5.  Select the target note you want to merge into and press `<CR>`.
6.  The right pane now becomes an editable buffer containing the **target note**.
7.  Manually copy content from the left (capture) pane to the right (target) pane. Make any edits you need.
8.  When you are finished, simply close the UI by pressing `q`.
    - The changes to the target note will be saved.
    - The original capture note will be archived without being copied to the destination.

## Commands

- `:PARAOrganize start`: Begin an organizing session.
- `:PARAOrganize reindex`: Rebuild the note index.
- `:PARAOrganize stop`: Force-close the organizer UI.

## UI Keymaps

These keymaps are active only when the organizer UI is open.

- `q`: Close the organizer UI.

### In the Suggestions List:

- `<CR>`: Accept the selected suggestion and move the note.
- `/`: Open the Telescope folder finder to begin a **Merge Flow**.

### In the Folder Contents List:

- `<CR>`: Select a note to merge into.
