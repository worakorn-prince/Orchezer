@echo off
REM orchezer.bat - zero-install CLI entry point (Windows cmd).
REM Usage: orchezer init --demo   (PowerShell: .\orchezer init)
python "%~dp0scripts\cli.py" %*
