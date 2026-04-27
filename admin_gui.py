import streamlit as st
import pandas as pd
import sqlite3
from pathlib import Path

# Configurazione - Assicurati che il path sia corretto rispetto a dove si trova il .db
DB_FILE = "memory.sqllite" # Cambia con il nome reale del file creato da storage-data.py

def get_connection():
    return sqlite3.connect(DB_FILE)

def run_query(query, params=()):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        return cursor

st.set_page_config(page_title="Gestione Utenti Chatbot", layout="wide")

st.title("👥 Admin Panel - Gestione Utenti")
st.info(f"Database collegato: `{DB_FILE}`")

# --- VISUALIZZAZIONE DATI ---
st.subheader("Lista Utenti Attuali")
try:
    with get_connection() as conn:
        df = pd.read_sql("SELECT * FROM users", conn)
    
    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.warning("Nessun utente trovato nella tabella.")
except Exception as e:
    st.error(f"Errore nel caricamento dati: {e}")

# --- OPERAZIONI CRUD ---
tab1, tab2 = st.tabs(["➕ Aggiungi Utente", "🗑️ Rimuovi Utente"])

# Tab 1: Inserimento
with tab1:
    with st.form("add_user_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            t_id = st.text_input("Telegram ID")
            f_name = st.text_input("Nome Completo")
            email = st.text_input("Email *")
        with col2:
            role = st.selectbox("Ruolo", ["user", "admin"])
            phone = st.text_input("Cellulare *")
        
        submit = st.form_submit_button("Salva Utente")
        
        if submit:
            if not email or not phone:
                st.error("L'Email e il Cellulare sono obbligatori!")
            else:
                try:
                    run_query(
                        "INSERT INTO users (telegram_id, full_name, email, role, mobile_phone) VALUES (?, ?, ?, ?, ?)",
                        (t_id, f_name, email, role, phone)
                    )
                    st.success(f"Utente {t_id} aggiunto!")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Errore: Questo Telegram ID esiste già!")

# Tab 2: Cancellazione
with tab2:
    if not df.empty:
        user_to_delete = st.selectbox("Seleziona utente da eliminare", df['telegram_id'].tolist())
        confirm_delete = st.button("Elimina Utente", type="primary")
        
        if confirm_delete:
            run_query("DELETE FROM users WHERE telegram_id = ?", (user_to_delete,))
            st.warning(f"Utente {user_to_delete} eliminato.")
            st.rerun()
    else:
        st.write("Nulla da eliminare.")

# --- FOOTER ---
st.divider()
st.caption("Interfaccia Marco Agent")