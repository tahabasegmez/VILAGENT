<#
.SYNOPSIS
    Look inside VILAGENT's experience memory: remembered runs, lessons and operator notes.

.DESCRIPTION
    Reads <data dir>\.vilagent\memory.sqlite read-only and prints PowerShell objects, so you can
    filter and sort them like anything else (Where-Object, Sort-Object, Export-Csv, …).

    Nothing typed and no screenshot is ever stored in memory, so nothing here is sensitive; keys
    live in connections.db and are never read by this script.

    SQLite is read through the project's Python interpreter (PowerShell has no SQLite of its own).

.PARAMETER Show
    summary   counts, embedding models and dimensions, and rows still waiting for a vector (default)
    runs      remembered runs, newest first
    lessons   learned lessons and your own notes
    used      what one run was given at recall time (needs -RunId)
    search    what recall would find for a phrase (needs -Query; keyword match only)

.PARAMETER Query
    The phrase for -Show search.

.PARAMETER RunId
    The run for -Show used. Run ids are in <data dir>\runs\ and in the runs listing.

.PARAMETER Path
    A memory.sqlite elsewhere (default: this repo's, else %APPDATA%\VILAGENT).

.PARAMETER Python
    The interpreter to read SQLite with (default: VILAGENT_PYTHON from .env, else python).

.PARAMETER Full
    Print whole lesson and task texts instead of one trimmed line.

.PARAMETER Raw
    Return the rows as objects instead of a printed table, for piping (Where-Object, Export-Csv, …).

.EXAMPLE
    .\tools\inspect-memory.ps1
.EXAMPLE
    .\tools\inspect-memory.ps1 -Show lessons -Full
.EXAMPLE
    .\tools\inspect-memory.ps1 -Show lessons | Where-Object source -eq operator
.EXAMPLE
    .\tools\inspect-memory.ps1 -Show used -RunId operator-run-7e460bc0-f1e1-4ab6-98ec-da95fc42c598
.EXAMPLE
    .\tools\inspect-memory.ps1 -Show search -Query "the verge"
.EXAMPLE
    .\tools\inspect-memory.ps1 -Show runs | Export-Csv runs.csv -NoTypeInformation
#>
[CmdletBinding()]
param(
    [ValidateSet('summary', 'runs', 'lessons', 'used', 'search')]
    [string]$Show = 'summary',
    [string]$Query,
    [string]$RunId,
    [string]$Path,
    [string]$Python,
    [switch]$Full,
    [switch]$Raw
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Resolve-MemoryDb {
    if ($Path) {
        if (-not (Test-Path $Path)) { throw "No memory database at $Path" }
        return (Resolve-Path $Path).Path
    }
    $candidates = @(
        (Join-Path $repo '.vilagent\memory.sqlite'),                       # running from source
        (Join-Path $env:APPDATA 'VILAGENT\.vilagent\memory.sqlite')        # the installed app
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
    }
    throw "No memory.sqlite found. Looked in:`n  $($candidates -join "`n  ")`nPass -Path to point at one."
}

function Resolve-Python {
    if ($Python) { return $Python }
    $envFile = Join-Path $repo '.env'
    if (Test-Path $envFile) {
        # Only this one line is read; keys in .env are never touched.
        $line = Select-String -Path $envFile -Pattern '^\s*VILAGENT_PYTHON\s*=\s*(.+)$' | Select-Object -First 1
        if ($line) {
            $exe = $line.Matches[0].Groups[1].Value.Trim().Trim('"')
            if (Test-Path $exe) { return $exe }
        }
    }
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) { throw "No Python found. Set VILAGENT_PYTHON in .env or pass -Python." }
    return $found.Source
}

$reader = @'
"""Read VILAGENT's memory store and print one JSON document (argv: db show query run_id)."""
import json
import sqlite3
import sys

db_path, show, query, run_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
db.row_factory = sqlite3.Row


def rows(sql, *params):
    return [dict(row) for row in db.execute(sql, params)]


def vectors(table):
    out = []
    for row in db.execute(f"SELECT embedding_model AS model, length(embedding) AS size FROM {table}"):
        out.append({"model": row["model"], "dims": (row["size"] or 0) // 4})
    return out


def lesson_rows(where="", *params):
    return rows(
        "SELECT id, source, key_type, key, text, hits, disabled, created_at, updated_at,"
        " embedding_model, length(embedding)/4 AS dims FROM lessons " + where + " ORDER BY source DESC, hits DESC",
        *params,
    )


def episode_rows(where="", *params):
    return rows(
        "SELECT id, run_id, created_at, task_text, approach, apps, domains, outcome, verified, rating,"
        " actions, duration_s, uses, embedding_model, length(embedding)/4 AS dims FROM episodes " + where + " ORDER BY created_at DESC",
        *params,
    )


if show == "summary":
    counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("episodes", "lessons")}
    models, waiting = {}, {"episodes": 0, "lessons": 0}
    for table in ("episodes", "lessons"):
        for item in vectors(table):
            if item["model"] is None:
                waiting[table] += 1
            else:
                key = f'{item["model"]} ({item["dims"]} dims)'
                models[key] = models.get(key, 0) + 1
    answer = {
        "database": db_path,
        "episodes": counts["episodes"],
        "lessons": counts["lessons"],
        "operator_notes": db.execute("SELECT COUNT(*) FROM lessons WHERE source = 'operator'").fetchone()[0],
        "disabled_lessons": db.execute("SELECT COUNT(*) FROM lessons WHERE disabled = 1").fetchone()[0],
        "bad_rated_runs": db.execute("SELECT COUNT(*) FROM episodes WHERE rating = 'bad'").fetchone()[0],
        "embedded_by": models,
        "waiting_for_a_vector": waiting,
        "keys": [dict(row) for row in db.execute("SELECT key_type, key, COUNT(*) AS lessons FROM lessons GROUP BY key_type, key ORDER BY lessons DESC")],
    }
elif show == "runs":
    answer = episode_rows()
elif show == "lessons":
    answer = lesson_rows()
elif show == "used":
    used = rows("SELECT entry_type, entry_id, rank FROM memory_usage WHERE run_id = ? ORDER BY entry_type, rank", run_id)
    episodes = [row for row in used if row["entry_type"] == "episodes"]
    lessons = [row for row in used if row["entry_type"] == "lessons"]
    answer = {
        "run_id": run_id,
        "episodes": episode_rows("WHERE id IN (%s)" % ",".join("?" * len(episodes)), *[row["entry_id"] for row in episodes]) if episodes else [],
        "lessons": lesson_rows("WHERE id IN (%s)" % ",".join("?" * len(lessons)), *[row["entry_id"] for row in lessons]) if lessons else [],
    }
else:  # search: the same full-text index recall uses (its vector half needs the running gateway)
    # Bare terms are AND-ed by FTS5, which is what "find entries about this" means.
    terms = " ".join(part for part in query.replace('"', " ").replace("*", " ").split() if part.upper() not in {"AND", "OR", "NOT", "NEAR"})

    def matched(table):
        found = rows(f"SELECT t.id FROM {table}_fts f JOIN {table} t ON t.rowid = f.rowid WHERE {table}_fts MATCH ? ORDER BY bm25({table}_fts)", terms)
        return [row["id"] for row in found]

    found_episodes, found_lessons = matched("episodes"), matched("lessons")
    answer = {
        "query": query,
        "episodes": episode_rows("WHERE id IN (%s)" % ",".join("?" * len(found_episodes)), *found_episodes) if found_episodes else [],
        "lessons": lesson_rows("WHERE id IN (%s)" % ",".join("?" * len(found_lessons)), *found_lessons) if found_lessons else [],
    }

print(json.dumps(answer, ensure_ascii=False, default=str))
'@

if ($Show -eq 'used' -and -not $RunId) { throw "-Show used needs -RunId." }
if ($Show -eq 'search' -and -not $Query) { throw "-Show search needs -Query." }

$database = Resolve-MemoryDb
$interpreter = Resolve-Python
$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ("vilagent-memory-" + [guid]::NewGuid().ToString('N') + ".py")
Set-Content -Path $scratch -Value $reader -Encoding UTF8
try {
    $env:PYTHONNOUSERSITE = '1'   # an older langchain in the user site-packages must not shadow ours
    $json = & $interpreter $scratch $database $Show $Query $RunId
    if ($LASTEXITCODE -ne 0) { throw "Could not read $database (exit $LASTEXITCODE)." }
} finally {
    Remove-Item $scratch -ErrorAction SilentlyContinue
}

$data = $json | ConvertFrom-Json

function Trim-Text([string]$text, [int]$width = 80) {
    if ($Full -or -not $text) { return $text }
    $flat = ($text -replace '\s+', ' ').Trim()
    if ($flat.Length -le $width) { return $flat }
    return $flat.Substring(0, $width - 1) + '…'
}

function Write-Rows($rows) {
    # A table is easier to read; -Raw hands the objects on so they can be filtered and exported.
    if ($Raw) { $rows } else { $rows | Format-Table -AutoSize }
}

function Show-Lessons($lessons, [string]$title) {
    if ($title) { Write-Host "`n$title" -ForegroundColor Magenta }
    if (-not $lessons) { Write-Host '  (none)' -ForegroundColor DarkGray; return }
    $lessons | ForEach-Object {
        [pscustomobject]@{
            source   = $_.source
            key      = "$($_.key_type):$($_.key)"
            hits     = $_.hits
            state    = if ($_.disabled) { 'off' } else { 'on' }
            vector   = if ($_.dims) { "$($_.dims) dims" } else { 'none yet' }
            text     = Trim-Text $_.text
            id       = $_.id
        }
    }
}

function Show-Runs($episodes, [string]$title) {
    if ($title) { Write-Host "`n$title" -ForegroundColor Magenta }
    if (-not $episodes) { Write-Host '  (none)' -ForegroundColor DarkGray; return }
    $episodes | ForEach-Object {
        [pscustomobject]@{
            when     = ([datetime]$_.created_at).ToLocalTime().ToString('yyyy-MM-dd HH:mm')
            outcome  = $_.outcome
            checked  = if ($_.verified) { 'yes' } else { 'no' }
            rating   = if ($_.rating) { $_.rating } else { '-' }
            actions  = $_.actions
            recalled = $_.uses
            vector   = if ($_.dims) { "$($_.dims) dims" } else { 'none yet' }
            task     = Trim-Text $_.task_text
            run_id   = $_.run_id
        }
    }
}

switch ($Show) {
    'summary' {
        Write-Host "`nVILAGENT memory" -ForegroundColor Magenta
        Write-Host "  $($data.database)" -ForegroundColor DarkGray
        [pscustomobject]@{
            Runs             = $data.episodes
            Lessons          = $data.lessons
            'Your notes'     = $data.operator_notes
            'Switched off'   = $data.disabled_lessons
            'Rated bad'      = $data.bad_rated_runs
        } | Format-List

        Write-Host 'Embedded by' -ForegroundColor Magenta
        if ($data.embedded_by.PSObject.Properties.Count -eq 0) {
            Write-Host '  nothing yet — memory searches by keyword only' -ForegroundColor DarkGray
        } else {
            $data.embedded_by.PSObject.Properties | ForEach-Object { [pscustomobject]@{ model = $_.Name; rows = $_.Value } } | Format-Table -AutoSize
        }
        $waiting = $data.waiting_for_a_vector.episodes + $data.waiting_for_a_vector.lessons
        if ($waiting -gt 0) {
            Write-Host "  $waiting row(s) have no vector yet; they are embedded the next time the gateway starts." -ForegroundColor Yellow
        }

        Write-Host "`nLessons filed under" -ForegroundColor Magenta
        $data.keys | ForEach-Object { [pscustomobject]@{ key = "$($_.key_type):$($_.key)"; lessons = $_.lessons } } | Format-Table -AutoSize
    }
    'runs'    { Write-Rows (Show-Runs $data) }
    'lessons' { Write-Rows (Show-Lessons $data) }
    'used'    {
        Write-Host "`nWhat run $($data.run_id) was given at recall" -ForegroundColor Magenta
        Write-Rows (Show-Runs $data.episodes 'Runs it was shown')
        Write-Rows (Show-Lessons $data.lessons 'Lessons it was given')
    }
    'search'  {
        Write-Host "`nFull-text matches for '$($data.query)'" -ForegroundColor Magenta
        Write-Host '  (recall also ranks by meaning; that half needs the gateway and its embeddings model)' -ForegroundColor DarkGray
        Write-Rows (Show-Runs $data.episodes 'Runs')
        Write-Rows (Show-Lessons $data.lessons 'Lessons')
    }
}
