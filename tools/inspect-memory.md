.\tools\inspect-memory.ps1                              # summary: counts, embedding model, keys
.\tools\inspect-memory.ps1 -Show lessons                # lessons + your notes
.\tools\inspect-memory.ps1 -Show lessons -Full          # untruncated text
.\tools\inspect-memory.ps1 -Show runs                   # remembered runs, newest first
.\tools\inspect-memory.ps1 -Show search -Query "verge"  # what the keyword half of recall finds
.\tools\inspect-memory.ps1 -Show used -RunId <run id>   # exactly what one run was handed
.\tools\inspect-memory.ps1 -Show runs -Raw | Where-Object outcome -ne success   # -Raw = objects