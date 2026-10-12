// SPDX-License-Identifier: GPL-3.0-or-later
/* Lanceur de la version portable : démarre runtime\bin\pythonw.exe app\lancer.py
   depuis le dossier de l'exécutable, sans fenêtre de console. */
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prec, PWSTR args, int affichage)
{
    wchar_t dossier[MAX_PATH], commande[4 * MAX_PATH + 64];
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    wchar_t *sep;

    (void)inst; (void)prec; (void)affichage;
    GetModuleFileNameW(NULL, dossier, MAX_PATH);
    sep = wcsrchr(dossier, L'\\');
    if (sep) *sep = 0;
    _snwprintf(commande, sizeof(commande) / sizeof(wchar_t) - 1,
               L"\"%ls\\runtime\\bin\\pythonw.exe\" \"%ls\\app\\lancer.py\" %ls", dossier, dossier, args);
    commande[sizeof(commande) / sizeof(wchar_t) - 1] = 0;

    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    if (!CreateProcessW(NULL, commande, NULL, NULL, FALSE, 0, NULL, dossier, &si, &pi)) {
        MessageBoxW(NULL, L"Impossible de lancer Python (dossier runtime manquant ?).",
                    L"MBTiles Fusion", MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
