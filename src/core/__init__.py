# VILAGENT Core

# -----------------------------------------------------------------------------
# DERS NOTU (src/core paketine genel bakış)
# -----------------------------------------------------------------------------
# Bu paket, ajanınızın “çekirdek yürütme döngüsünü” (planla → algıla → politika
# kontrolü → aksiyon al → doğrula → gerekirse toparlan/recover) tanımlar.
#
# Bu klasördeki dosyaların rol ayrımı bilinçlidir:
# - state.py    : Tüm veri sözleşmeleri (AgentState, Plan, ToolCall, Telemetry…)
# - nodes.py    : Her bir düğümün (node) iş mantığı (state mutasyonu)
# - edges.py    : Koşullu yönlendirme (state -> bir sonraki node anahtarı)
# - workflow.py : LangGraph entegrasyonu; graph wiring + dependency injection
# - local_tools.py : Lokal çalıştırılması güvenli/minimal tool implementasyonları
#
# Bu ayrım sayesinde:
# - Test edilebilirlik artar (nodes/edges framework bağımsızdır).
# - Araç çağrıları tek noktadan denetlenir (ToolExecutor + policy).
# - Denetlenebilirlik/audit korunur (ActionRecord append-only).
#
# Not: Bu dosya (package __init__) özellikle minimal tutulmuş.
# Ama “çekirdek” kavramı, burada tanımlı state ve state-makinesi akışını ifade eder.
