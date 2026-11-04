# Generates the synthetic user utterances used by the voice-lab scenarios.
# Uses the Windows SAPI voices; no network access.
param(
  [string]$OutDir = "$PSScriptRoot\audio",
  [string]$Voice = "Microsoft Huihui Desktop"
)

Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$utterances = [ordered]@{
  "q_single"      = "你好，请用一句话介绍一下你自己。"
  "q_multi1"      = "你最近在读什么书？"
  "q_multi2"      = "那本书里最打动你的是什么？"
  "q_multi3"      = "如果让你推荐一个习惯，你会推荐什么？"
  "q_long"        = "请详细讲讲你对人工智能未来的看法，尽量多说几句。"
  "q_long_detail" = "请给我详细讲讲你一天的工作安排，从早到晚，多说一些细节。"
  "q_barge"       = "等一下，我想换个话题。"
  "q_backchannel" = "嗯，对。"
}

$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
  16000,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
  [System.Speech.AudioFormat.AudioChannel]::Mono
)

foreach ($name in $utterances.Keys) {
  $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $synth.SelectVoice($Voice)
  $synth.Rate = 0
  $path = Join-Path $OutDir "$name.wav"
  $synth.SetOutputToWaveFile($path, $format)
  $synth.Speak($utterances[$name])
  $synth.Dispose()
  $info = Get-Item $path
  Write-Host ("{0,-16} {1,8:N0} bytes" -f $name, $info.Length)
}
