from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from admin_auth import AdminAuth


class ResetAdminWindow:
    def __init__(self) -> None:
        self.auth = AdminAuth(Path(__file__).resolve().parent)
        self.root = tk.Tk()
        self.root.title("Project Neunggureongi - 관리자 초기화")
        self.root.geometry("460x430")
        self.root.resizable(False, False)
        self.root.configure(bg="#101b33")
        panel = tk.Frame(self.root, bg="white", padx=34, pady=30)
        panel.pack(fill="both", expand=True, padx=24, pady=24)
        tk.Label(panel, text="관리자 계정 초기화", bg="white", fg="#172033",
                 font=("Malgun Gothic", 18, "bold")).pack(anchor="w")
        tk.Label(panel, text="VERSION 1.0.0", bg="white", fg="#28664d",
                 font=("Arial", 9, "bold")).pack(anchor="w", pady=(2, 10))
        tk.Label(
            panel,
            text="새 계정을 저장하면 기존 로그인 세션이 모두 종료되고,\n"
                 "이전 인증 파일은 백업 폴더에 보관됩니다.",
            bg="white", fg="#667085", justify="left",
            font=("Malgun Gothic", 9),
        ).pack(anchor="w", pady=(0, 15))
        self.username = self._field(panel, "새 관리자 ID")
        self.password = self._field(panel, "새 비밀번호 (8자 이상)", True)
        self.confirmation = self._field(panel, "비밀번호 확인", True)
        tk.Button(
            panel, text="관리자 계정 초기화", command=self.reset,
            bg="#28664d", fg="white", activebackground="#1e523d",
            activeforeground="white", relief="flat", cursor="hand2",
            font=("Malgun Gothic", 10, "bold"), pady=10,
        ).pack(fill="x", pady=(22, 0))
        self.username.focus_set()
        self.root.bind("<Return>", lambda _event: self.reset())

    @staticmethod
    def _field(parent: tk.Widget, label: str, password: bool = False) -> tk.Entry:
        tk.Label(parent, text=label, bg="white", fg="#344054",
                 font=("Malgun Gothic", 9, "bold")).pack(anchor="w", pady=(10, 4))
        entry = tk.Entry(parent, show="*" if password else "", relief="solid",
                         bd=1, font=("Malgun Gothic", 10))
        entry.pack(fill="x", ipady=7)
        return entry

    def reset(self) -> None:
        username = self.username.get().strip()
        password = self.password.get()
        if password != self.confirmation.get():
            messagebox.showerror("입력 오류", "비밀번호 확인이 일치하지 않습니다.")
            return
        if not messagebox.askyesno(
            "초기화 확인", "관리자 계정을 초기화하고 기존 로그인 세션을 모두 종료할까요?"
        ):
            return
        try:
            backup = self.auth.reset_credentials(username, password)
        except Exception as error:
            messagebox.showerror("초기화 실패", str(error))
            return
        detail = f"\n\n기존 인증 정보 백업:\n{backup}" if backup else ""
        messagebox.showinfo(
            "초기화 완료",
            "새 관리자 계정이 저장되었습니다.\n서버를 재시작한 후 로그인하세요." + detail,
        )
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    ResetAdminWindow().run()
