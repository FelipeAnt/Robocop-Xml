"""
configurador.py
----------------
Interface gráfica simples para configurar o robocop_clone.py:
- Adicionar/remover pastas a monitorar (ex: uma para NFC-e, outra para NF-e)
- Definir a data de início do upload para cada pasta
- Definir as credenciais do Cloudflare R2 (Account ID, Access Key, Secret Key, bucket)

Gera/atualiza o arquivo config.json, lido pelo robocop_clone.py.
Não precisa de nenhuma biblioteca extra (Tkinter já vem com o Python padrão
do Windows/python.org).
"""

import json
import os
import re
import shutil
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import segredo

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent
CONFIG_FILE = str(BASE_DIR / "config.json")


def _config_padrao():
    return {
        "fontes": [],
        "r2_account_id": "",
        "r2_access_key_id": "",
        "r2_secret_access_key": "",
        "r2_bucket": "",
        "num_workers": 4,
    }


def carregar_config_existente():
    """Devolve (config, aviso). Se o config.json existir mas estiver ilegível,
    guarda uma cópia 'config.json.corrompido-<data>' (para não perder nada que
    dê pra recuperar) e começa de um modelo em branco, avisando o usuário."""
    path = Path(CONFIG_FILE)
    if not path.exists():
        return _config_padrao(), None
    try:
        with open(path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("o conteúdo não é um objeto JSON")
        return cfg, None
    except Exception as e:
        copia = path.with_name(f"config.json.corrompido-{datetime.now():%Y%m%d-%H%M%S}")
        try:
            shutil.copy2(path, copia)
            onde = f"Uma cópia do arquivo antigo foi guardada em:\n{copia}"
        except OSError:
            onde = "Não consegui guardar uma cópia do arquivo antigo."
        return _config_padrao(), (
            f"O config.json existente está ilegível ({e}).\n\n{onde}\n\n"
            f"Preencha tudo de novo e salve para recriar a configuração."
        )


def salvar_config_atomico(cfg: dict):
    """Grava em arquivo temporário e troca: queda de energia no meio do save
    não deixa o config.json pela metade (o serviço ficaria sem configuração)."""
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


class ConfiguradorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Configurador - Robocop Clone")
        self.root.geometry("720x640")
        self.root.resizable(False, False)

        self.cfg, aviso_cfg = carregar_config_existente()

        self._montar_widgets()
        self._atualizar_lista_fontes()
        if aviso_cfg:
            self.root.after(200, lambda: messagebox.showwarning("Configuração antiga ilegível", aviso_cfg))

    # ------------------------------------------------------------
    def _montar_widgets(self):
        pad = {"padx": 10, "pady": 6}

        # --- Seção: pastas monitoradas ---
        frame_fontes = ttk.LabelFrame(self.root, text="Pastas monitoradas (NFC-e, NF-e, etc.)")
        frame_fontes.pack(fill="both", expand=True, **pad)

        colunas = ("nome", "pasta", "tipo", "data_inicio")
        self.tree = ttk.Treeview(frame_fontes, columns=colunas, show="headings", height=8)
        self.tree.heading("nome", text="Nome")
        self.tree.heading("pasta", text="Pasta")
        self.tree.heading("tipo", text="Tipo")
        self.tree.heading("data_inicio", text="Início do upload")
        self.tree.column("nome", width=130)
        self.tree.column("pasta", width=310)
        self.tree.column("tipo", width=90, anchor="center")
        self.tree.column("data_inicio", width=110, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        frame_botoes_fonte = ttk.Frame(frame_fontes)
        frame_botoes_fonte.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(frame_botoes_fonte, text="+ Adicionar pasta", command=self._adicionar_fonte).pack(side="left")
        ttk.Button(frame_botoes_fonte, text="Editar data de início", command=self._editar_data).pack(side="left", padx=8)
        ttk.Button(frame_botoes_fonte, text="Editar tipo", command=self._editar_tipo).pack(side="left")
        ttk.Button(frame_botoes_fonte, text="Remover selecionada", command=self._remover_fonte).pack(side="left", padx=8)

        # --- Seção: configurações gerais ---
        frame_geral = ttk.LabelFrame(self.root, text="Configurações do Cloudflare R2")
        frame_geral.pack(fill="x", **pad)

        ttk.Label(frame_geral, text="Account ID:").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.entry_account_id = ttk.Entry(frame_geral, width=45)
        self.entry_account_id.grid(row=0, column=1, sticky="w", padx=8, pady=4)
        self.entry_account_id.insert(0, self.cfg.get("r2_account_id", ""))

        ttk.Label(frame_geral, text="Access Key ID:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.entry_access_key = ttk.Entry(frame_geral, width=45)
        self.entry_access_key.grid(row=1, column=1, sticky="w", padx=8, pady=4)
        self.entry_access_key.insert(0, self.cfg.get("r2_access_key_id", ""))

        ttk.Label(frame_geral, text="Secret Access Key:").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        self.entry_secret_key = ttk.Entry(frame_geral, width=45, show="*")
        self.entry_secret_key.grid(row=2, column=1, sticky="w", padx=8, pady=4)
        try:
            secret_atual = segredo.revelar(self.cfg.get("r2_secret_access_key", ""))
        except segredo.SegredoIndisponivel:
            secret_atual = ""
            messagebox.showwarning(
                "Secret não abre neste PC",
                "O Secret Access Key salvo no config.json foi criptografado em outro "
                "computador e não abre aqui. Digite o Secret novamente e salve.",
            )
        self.entry_secret_key.insert(0, secret_atual)

        ttk.Label(frame_geral, text="Nome do bucket:").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        self.entry_bucket = ttk.Entry(frame_geral, width=45)
        self.entry_bucket.grid(row=3, column=1, sticky="w", padx=8, pady=4)
        self.entry_bucket.insert(0, self.cfg.get("r2_bucket", ""))

        ttk.Label(
            frame_geral,
            text="Essas informações vêm do painel Cloudflare: R2 → Manage R2 API Tokens.",
            foreground="#555555",
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))

        ttk.Label(frame_geral, text="Uploads simultâneos (workers):").grid(row=5, column=0, sticky="w", padx=8, pady=4)
        self.spin_workers = ttk.Spinbox(frame_geral, from_=1, to=20, width=5)
        self.spin_workers.set(self.cfg.get("num_workers", 4))
        self.spin_workers.grid(row=5, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(frame_geral, text="Nome do cliente:").grid(row=6, column=0, sticky="w", padx=8, pady=4)
        self.entry_cliente = ttk.Entry(frame_geral, width=45)
        self.entry_cliente.grid(row=6, column=1, sticky="w", padx=8, pady=4)
        self.entry_cliente.insert(0, self.cfg.get("nome_cliente", ""))
        ttk.Label(
            frame_geral,
            text="Aparece no monitoramento (heartbeat). Ex: 'Mercado Zilda'. Use um nome diferente por PC.",
            foreground="#555555",
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))

        # --- Botão salvar ---
        frame_salvar = ttk.Frame(self.root)
        frame_salvar.pack(fill="x", padx=10, pady=10)
        ttk.Button(frame_salvar, text="Salvar configuração", command=self._salvar).pack(side="right")

    # ------------------------------------------------------------
    def _atualizar_lista_fontes(self):
        self.tree.delete(*self.tree.get_children())
        for i, fonte in enumerate(self.cfg.get("fontes", [])):
            tipo_label = "Entrada" if fonte.get("tipo", "saida") == "entrada" else "Saída"
            self.tree.insert("", "end", iid=str(i), values=(
                fonte.get("nome", ""), fonte.get("pasta", ""), tipo_label, fonte.get("data_inicio", "")
            ))

    def _escolher_tipo(self, tipo_atual="saida"):
        """Janela simples com dois botões para escolher saída (emitente) ou
        entrada (destinatário). Retorna 'saida', 'entrada', ou None se cancelado."""
        janela = tk.Toplevel(self.root)
        janela.title("Tipo de nota")
        janela.geometry("420x220")
        janela.resizable(False, False)
        janela.transient(self.root)
        janela.grab_set()

        resultado = {"valor": None}
        var = tk.StringVar(value=tipo_atual)

        ttk.Label(
            janela,
            text="Essa pasta guarda notas de que tipo?",
            font=("TkDefaultFont", 10, "bold"),
        ).pack(anchor="w", padx=16, pady=(16, 8))

        ttk.Radiobutton(
            janela, text="Saída — notas emitidas pelo cliente (NFC-e/NF-e)\norganiza pelo CNPJ do emitente",
            variable=var, value="saida",
        ).pack(anchor="w", padx=16, pady=4)

        ttk.Radiobutton(
            janela, text="Entrada — notas de compra/bonificação recebidas\n(organiza pelo CNPJ do destinatário, ou seja, do cliente)",
            variable=var, value="entrada",
        ).pack(anchor="w", padx=16, pady=4)

        def confirmar():
            resultado["valor"] = var.get()
            janela.destroy()

        def cancelar():
            janela.destroy()

        frame_botoes = ttk.Frame(janela)
        frame_botoes.pack(side="bottom", pady=16)
        ttk.Button(frame_botoes, text="OK", command=confirmar).pack(side="left", padx=8)
        ttk.Button(frame_botoes, text="Cancelar", command=cancelar).pack(side="left")

        janela.wait_window()
        return resultado["valor"]

    def _adicionar_fonte(self):
        pasta = filedialog.askdirectory(title="Selecione a pasta onde o XML é salvo")
        if not pasta:
            return

        nome = simpledialog.askstring(
            "Nome da fonte",
            "Um nome pra identificar essa pasta (ex: 'NFC-e Loja 1', 'Entradas Loja 1'):",
            initialvalue=Path(pasta).name,
        )
        if nome is None:
            return

        tipo = self._escolher_tipo("saida")
        if tipo is None:
            return

        data_str = simpledialog.askstring(
            "Data de início",
            "A partir de qual data (AAAA-MM-DD) esse robô deve começar a subir arquivos?\n"
            "Só XMLs com data de emissão igual ou posterior serão enviados.",
            initialvalue=datetime.now().strftime("%Y-%m-01"),
        )
        if data_str is None:
            return

        try:
            datetime.strptime(data_str.strip(), "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("Data inválida", "Use o formato AAAA-MM-DD, por exemplo 2026-06-01.")
            return

        self.cfg.setdefault("fontes", []).append({
            "nome": nome.strip() or Path(pasta).name,
            "pasta": pasta,
            "data_inicio": data_str.strip(),
            "tipo": tipo,
        })
        self._atualizar_lista_fontes()

    def _editar_tipo(self):
        selecionado = self.tree.selection()
        if not selecionado:
            messagebox.showinfo("Nada selecionado", "Selecione uma pasta na lista primeiro.")
            return
        idx = int(selecionado[0])
        fonte = self.cfg["fontes"][idx]

        novo_tipo = self._escolher_tipo(fonte.get("tipo", "saida"))
        if novo_tipo is None:
            return

        fonte["tipo"] = novo_tipo
        self._atualizar_lista_fontes()

    def _editar_data(self):
        selecionado = self.tree.selection()
        if not selecionado:
            messagebox.showinfo("Nada selecionado", "Selecione uma pasta na lista primeiro.")
            return
        idx = int(selecionado[0])
        fonte = self.cfg["fontes"][idx]

        nova_data = simpledialog.askstring(
            "Editar data de início",
            f"Nova data de início (AAAA-MM-DD) para '{fonte['nome']}':",
            initialvalue=fonte["data_inicio"],
        )
        if nova_data is None:
            return
        try:
            datetime.strptime(nova_data.strip(), "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("Data inválida", "Use o formato AAAA-MM-DD.")
            return

        fonte["data_inicio"] = nova_data.strip()
        self._atualizar_lista_fontes()

    def _remover_fonte(self):
        selecionado = self.tree.selection()
        if not selecionado:
            messagebox.showinfo("Nada selecionado", "Selecione uma pasta na lista primeiro.")
            return
        idx = int(selecionado[0])
        fonte = self.cfg["fontes"][idx]
        if messagebox.askyesno("Confirmar", f"Remover o monitoramento de '{fonte['nome']}'?"):
            del self.cfg["fontes"][idx]
            self._atualizar_lista_fontes()

    # ------------------------------------------------------------
    def _salvar(self):
        account_id = self.entry_account_id.get().strip()
        access_key = self.entry_access_key.get().strip()
        secret_key = self.entry_secret_key.get().strip()
        bucket = self.entry_bucket.get().strip()
        nome_cliente = self.entry_cliente.get().strip()

        if not self.cfg.get("fontes"):
            messagebox.showerror("Nenhuma pasta", "Adicione ao menos uma pasta para monitorar.")
            return
        if not nome_cliente:
            messagebox.showerror("Nome do cliente", "Informe o nome do cliente (usado no monitoramento).")
            return
        if not (account_id and access_key and secret_key and bucket):
            messagebox.showerror(
                "Campos obrigatórios",
                "Preencha Account ID, Access Key ID, Secret Access Key e nome do bucket.",
            )
            return

        if not re.fullmatch(r"[0-9a-fA-F]{32}", account_id):
            messagebox.showerror(
                "Account ID inválido",
                "O Account ID deve ter 32 caracteres (letras a-f e números).\n"
                "Copie só o ID, sem 'https://' nem '.r2.cloudflarestorage.com'.",
            )
            return
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            messagebox.showerror(
                "Bucket inválido",
                "Nome do bucket: 3 a 63 caracteres, só minúsculas, números e hífen.",
            )
            return
        try:
            workers = int(self.spin_workers.get())
        except ValueError:
            messagebox.showerror("Workers", "Número de workers inválido.")
            return

        self.cfg["r2_account_id"] = account_id
        self.cfg["r2_access_key_id"] = access_key
        aviso_segredo = ""
        try:
            protegido = segredo.proteger(secret_key)
            if segredo.revelar(protegido) != secret_key:
                raise RuntimeError("verificação falhou")
            self.cfg["r2_secret_access_key"] = protegido
            if segredo.esta_protegido(protegido):
                aviso_segredo = "\n\nO Secret foi criptografado: só funciona neste computador."
        except Exception as e:
            self.cfg["r2_secret_access_key"] = secret_key
            aviso_segredo = f"\n\nATENÇÃO: não consegui criptografar o Secret ({e}); ficou em texto puro."
        self.cfg["r2_bucket"] = bucket
        self.cfg["num_workers"] = max(1, min(32, workers))
        self.cfg["nome_cliente"] = nome_cliente
        self.cfg.setdefault("heartbeat_minutos", 15)
        self.cfg.setdefault("exigir_protocolo", True)

        try:
            salvar_config_atomico(self.cfg)
        except OSError as e:
            messagebox.showerror(
                "Não consegui salvar",
                f"Erro ao gravar {CONFIG_FILE}:\n{e}\n\n"
                f"Confira se a pasta permite escrita (rode como administrador).",
            )
            return

        messagebox.showinfo(
            "Salvo",
            f"Configuração salva em {CONFIG_FILE}.\n\n"
            f"Agora rode robocop_clone.py para iniciar o monitoramento "
            f"(sem login nenhum — as credenciais já estão no config.json)."
            + aviso_segredo
        )


def main():
    root = tk.Tk()
    ConfiguradorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
