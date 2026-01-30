import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px
from PIL import Image, ImageOps
import pytesseract
import re
from datetime import datetime
import json
from groq import Groq

# --- CONFIGURATION ---
DB_NAME = 'receipt_vault_v6.db'
GROQ_MODEL = "llama-3.3-70b-versatile" 

# --- DATABASE FUNCTIONS ---
def init_db():
    """Initializes the SQLite database."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    merchant TEXT,
                    date TEXT,
                    invoice_number TEXT,
                    subtotal REAL,
                    tax REAL,
                    total_amount REAL,
                    filename TEXT,
                    upload_timestamp TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS line_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id INTEGER,
                    name TEXT,
                    qty INTEGER,
                    price REAL,
                    FOREIGN KEY (receipt_id) REFERENCES receipts (id)
                )''')
    conn.commit()
    conn.close()

def check_if_receipt_exists(merchant, date, total, invoice_num):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    query = "SELECT id FROM receipts WHERE merchant = ? AND date = ? AND total_amount = ?"
    params = [merchant, date, total]
    
    if invoice_num and invoice_num != "Unknown":
        query += " AND invoice_number = ?"
        params.append(invoice_num)
        
    c.execute(query, tuple(params))
    data = c.fetchall()
    conn.close()
    return len(data) > 0, len(data) 

def save_receipt_to_db(data, filename, line_items_data):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    c.execute("""INSERT INTO receipts 
                 (merchant, date, invoice_number, subtotal, tax, total_amount, filename, upload_timestamp) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (data['merchant'], data['date'], data['invoice_number'], 
               data['subtotal'], data['tax'], data['total'], filename, upload_time))
    
    receipt_id = c.lastrowid
    for item in line_items_data:
        c.execute("INSERT INTO line_items (receipt_id, name, qty, price) VALUES (?, ?, ?, ?)",
                  (receipt_id, item['name'], item['qty'], item['price']))
    conn.commit()
    conn.close()
    return receipt_id

def get_all_receipts():
    conn = sqlite3.connect(DB_NAME)
    try:
        df = pd.read_sql_query("SELECT * FROM receipts ORDER BY id DESC", conn)
    except:
        df = pd.DataFrame()
    conn.close()
    return df

def get_line_items(receipt_id):
    """Fetches line items for a specific receipt ID."""
    conn = sqlite3.connect(DB_NAME)
    try:
        df = pd.read_sql_query("SELECT name, qty, price FROM line_items WHERE receipt_id = ?", conn, params=(receipt_id,))
    except:
        df = pd.DataFrame()
    conn.close()
    return df

def delete_receipt(receipt_id):
    """Deletes a receipt and its associated line items."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Delete line items first (Foreign Key hygiene)
    c.execute("DELETE FROM line_items WHERE receipt_id = ?", (receipt_id,))
    # Delete the main receipt
    c.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    conn.commit()
    conn.close()

def clear_database():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM line_items")
    c.execute("DELETE FROM receipts")
    conn.commit()
    conn.close()

# --- PROCESSING FUNCTIONS ---
def preprocess_image(image):
    return ImageOps.grayscale(image)

def extract_text(image):
    return pytesseract.image_to_string(image)

def parse_with_groq(raw_text, api_key):
    client = Groq(api_key=api_key)
    prompt = f"""
    Extract structured data from this receipt text. 
    Return ONLY a JSON object with these keys: 
    'merchant', 'date' (YYYY-MM-DD), 'invoice_number', 'subtotal', 'tax', 'total', 
    and 'line_items' (a list of objects with 'name', 'qty', 'price').
    
    If 'subtotal' is missing but 'total' and 'tax' exist, calculate it.
    If 'tax' is missing, try to infer it or set to 0.
    
    Text:
    {raw_text}
    """
    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=GROQ_MODEL,
            response_format={"type": "json_object"}
        )
        return json.loads(chat_completion.choices[0].message.content)
    except Exception as e:
        st.error(f"Groq Parsing Error: {e}")
        return None

# --- VALIDATION LOGIC ---
def validate_receipt(data, is_dup_bool):
    results = {}
    sub = data.get('subtotal', 0)
    tax = data.get('tax', 0)
    total = data.get('total', 0)
    calc_total = sub + tax
    
    if abs(calc_total - total) <= 0.03:
        results['math'] = (True, f"Pass: {sub:.2f} + {tax:.2f} = {total:.2f}")
    else:
        results['math'] = (False, f"Fail: {sub:.2f} + {tax:.2f} != {total:.2f}")

    if not is_dup_bool:
        results['dup'] = (True, f"No duplicate found")
    else:
        results['dup'] = (False, "Potential duplicate entry detected in DB.")

    if sub > 0:
        rate = (tax / sub) * 100
        if 0 <= rate <= 30:
            results['tax_rate'] = (True, f"Tax Rate: {rate:.1f}% (Normal)")
        else:
            results['tax_rate'] = (False, f"Suspicious Tax Rate: {rate:.1f}%")
    else:
         results['tax_rate'] = (True, "N/A")

    missing = []
    if data['merchant'] == "Unknown": missing.append("Merchant")
    if not data['date']: missing.append("Date")
    if data['total'] == 0.0: missing.append("Total")
    
    if not missing:
        results['fields'] = (True, "All required fields present")
    else:
        results['fields'] = (False, f"Missing: {', '.join(missing)}")

    return results

# --- MAIN APP ---
def main():
    st.set_page_config(page_title="Receipt Vault & Validator", layout="wide", page_icon="🧾")
    init_db()

    # Session State
    if 'current_receipt' not in st.session_state: st.session_state['current_receipt'] = None
    if 'current_line_items' not in st.session_state: st.session_state['current_line_items'] = []
    if 'validation_status' not in st.session_state: st.session_state['validation_status'] = None

    # --- SIDEBAR ---
    with st.sidebar:
        st.header("🔑 API Configuration")
        user_groq_key = st.text_input("Enter Groq API Key", type="password", help="Get your key at console.groq.com")
        
        st.divider()
        st.header("⚙️ Settings")
        if st.button("Clear Database"):
            clear_database()
            st.toast("Database cleared!", icon="🗑️")
            st.rerun()

    st.title("🧾 Receipt Vault System")
    
    # 
    
    # Updated Tabs
    tab_vault, tab_validation, tab_history, tab_analytics = st.tabs(["📤 Upload & Process", "✅ Extraction & Validation", "📜 Bill History", "📊 Analytics"])

    # === TAB 1: UPLOAD & PROCESS ===
    with tab_vault:
        st.markdown("### 1. Document Ingestion")
        
        uploaded_file = st.file_uploader("Upload Receipt", type=["png", "jpg", "jpeg"])
        
        if uploaded_file:
            image = Image.open(uploaded_file)
            cleaned_image = preprocess_image(image)

            st.subheader("Image Processing")
            col_img1, col_img2 = st.columns(2)
            with col_img1:
                st.image(image, caption="Original Receipt", use_container_width=True)
            with col_img2:
                st.image(cleaned_image, caption="Cleaned (Grayscale) for OCR", use_container_width=True)

            st.divider()

            if st.button("🚀 Extract & Process", type="primary", use_container_width=True):
                if not user_groq_key:
                    st.error("Please enter your Groq API Key in the sidebar first.")
                else:
                    with st.spinner("Running OCR & Groq AI Analysis..."):
                        raw_text = extract_text(cleaned_image)
                        structured_data = parse_with_groq(raw_text, user_groq_key)
                        
                        if structured_data:
                            receipt_data = {
                                "merchant": structured_data.get('merchant', 'Unknown'),
                                "date": structured_data.get('date', datetime.now().strftime("%Y-%m-%d")),
                                "invoice_number": structured_data.get('invoice_number', 'Unknown'),
                                "subtotal": float(structured_data.get('subtotal', 0)),
                                "tax": float(structured_data.get('tax', 0)),
                                "total": float(structured_data.get('total', 0))
                            }
                            line_items = structured_data.get('line_items', [])
                            
                            st.session_state['current_receipt'] = receipt_data
                            st.session_state['current_line_items'] = line_items
                            
                            is_dup, _ = check_if_receipt_exists(
                                receipt_data['merchant'], receipt_data['date'], 
                                receipt_data['total'], receipt_data['invoice_number']
                            )
                            val_results = validate_receipt(receipt_data, is_dup)
                            st.session_state['validation_status'] = val_results
                            
                            save_receipt_to_db(receipt_data, uploaded_file.name, line_items)
                            st.success(f"Processing Complete! Added to Vault.")
                            
                            st.markdown("#### Quick Validation Check")
                            v1, v2, v3 = st.columns(3)
                            v1.metric("Math Check", "Pass" if val_results['math'][0] else "Fail")
                            v2.metric("Duplicate", "None" if val_results['dup'][0] else "Found")
                            v3.metric("Tax Rate", "OK" if val_results['tax_rate'][0] else "Suspicious")
                        else:
                            st.error("AI could not parse the receipt.")

    # === TAB 2: DETAILED VALIDATION ===
    with tab_validation:
        st.markdown("## Field Extraction & Validation Details")
        
        if st.session_state['current_receipt']:
            data = st.session_state['current_receipt']
            items = st.session_state['current_line_items']
            val = st.session_state['validation_status']
            
            c_extract, c_validate, c_db = st.columns(3)
            
            with c_extract:
                st.info("🔹 Field Extraction")
                with st.container(border=True):
                    st.text_input("Vendor", value=data.get('merchant', ''), disabled=True)
                    st.text_input("Date", value=data.get('date', ''), disabled=True)
                    st.text_input("Invoice #", value=data.get('invoice_number', ''), disabled=True)
                    
                    st.markdown("---")
                    st.caption("Math Components")
                    c1, c2 = st.columns(2)
                    c1.text_input("Subtotal", value=f"{data.get('subtotal', 0):.2f}", disabled=True)
                    c2.text_input("Tax", value=f"{data.get('tax', 0):.2f}", disabled=True)
                    st.text_input("Total", value=f"{data.get('total', 0):.2f}", disabled=True)
                    
                    st.markdown("**Line Items:**")
                    if items:
                        st.dataframe(pd.DataFrame(items), hide_index=True, height=150)

            with c_validate:
                st.info("🔹 Validation Logic")
                if val:
                    res_math = val['math']
                    if res_math[0]:
                        st.success(f"**Math Check**: {res_math[1]}")
                    else:
                        st.error(f"**Math Check**: {res_math[1]}")
                        
                    for key in ['dup', 'tax_rate', 'fields']:
                        res = val[key]
                        if res[0]:
                            st.success(f"**{key.replace('_', ' ').title()}**: {res[1]}")
                        else:
                            st.error(f"**{key.replace('_', ' ').title()}**: {res[1]}")

            with c_db:
                st.info("🔹 Vault Status")
                df_all = get_all_receipts()
                if not df_all.empty:
                    st.metric("Total Vault Entries", len(df_all))
                    st.dataframe(df_all[['id', 'merchant', 'total_amount']].head(10), hide_index=True)
        else:
            st.warning("Please upload a document first.")

    # === TAB 3: BILL HISTORY (NEW) ===
    with tab_history:
        st.header("📜 Detailed Bill History & Management")
        
        df_history = get_all_receipts()
        
        if not df_history.empty:
            # Layout: Left for Selection, Right for Details
            col_list, col_detail = st.columns([1, 2])
            
            with col_list:
                st.subheader("Select Bill")
                bill_id_list = df_history['id'].tolist()
                
                # Create a label for the dropdown including merchant and amount
                df_history['label'] = df_history.apply(lambda x: f"ID: {x['id']} - {x['merchant']} (${x['total_amount']})", axis=1)
                selected_label = st.selectbox("Choose a Receipt to View/Manage:", df_history['label'])
                
                # Extract ID from selection
                selected_id = int(selected_label.split(" - ")[0].replace("ID: ", ""))
                
                st.divider()
                st.markdown("### Delete Bill")
                if st.button(f"Delete Bill ID: {selected_id}", type="primary"):
                    delete_receipt(selected_id)
                    st.toast(f"Receipt {selected_id} deleted!", icon="🗑️")
                    st.rerun()

            with col_detail:
                st.subheader("Bill Details")
                
                # Get specific row data
                selected_row = df_history[df_history['id'] == selected_id].iloc[0]
                
                # Display High Level Info
                with st.container(border=True):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Merchant", selected_row['merchant'])
                    c2.metric("Date", selected_row['date'])
                    c3.metric("Total Amount", f"${selected_row['total_amount']:.2f}")
                    
                    st.markdown(f"**Invoice #:** {selected_row['invoice_number']}")
                    st.markdown(f"**Uploaded:** {selected_row['upload_timestamp']}")

                # Display Line Items
                st.subheader("🛒 Line Items")
                line_items_df = get_line_items(selected_id)
                
                if not line_items_df.empty:
                    st.dataframe(line_items_df, use_container_width=True, hide_index=True)
                else:
                    st.info("No detailed line items found for this receipt.")
        else:
            st.info("No receipts found in the database.")

    # === TAB 4: ANALYTICS (UPDATED) ===
    with tab_analytics:
        st.subheader("Spend Analytics")
        df = get_all_receipts()
        
        if not df.empty:
            df['total_amount'] = pd.to_numeric(df['total_amount'], errors='coerce').fillna(0)
            
            # 1. Spending per Vendor (Bar)
            # 2. Spend Distribution (Pie)
            # 3. Monthly Spending (Line) - NEW
            
            col_a, col_b = st.columns(2)
            with col_a:
                fig = px.bar(df, x='merchant', y='total_amount', color='merchant', title="Spending per Vendor")
                st.plotly_chart(fig, use_container_width=True)
            with col_b:
                fig2 = px.pie(df, values='total_amount', names='merchant', title="Spend Distribution")
                st.plotly_chart(fig2, use_container_width=True)
            
            st.divider()
            
            # Process Date for Monthly Graph
            try:
                # 
                df['date_obj'] = pd.to_datetime(df['date'], errors='coerce')
                # Remove invalid dates
                df_time = df.dropna(subset=['date_obj']).copy()
                df_time['month_year'] = df_time['date_obj'].dt.strftime('%Y-%m')
                
                # Group by Month
                monthly_spend = df_time.groupby('month_year')['total_amount'].sum().reset_index()
                monthly_spend = monthly_spend.sort_values('month_year')

                st.subheader("📅 Monthly Spending Trend")
                if not monthly_spend.empty:
                    fig3 = px.line(monthly_spend, x='month_year', y='total_amount', markers=True, 
                                   title="Total Spending Over Time",
                                   labels={'month_year': 'Month', 'total_amount': 'Amount ($)'})
                    st.plotly_chart(fig3, use_container_width=True)
                else:
                    st.warning("Dates provided in receipts are not valid for time-series analysis.")
            except Exception as e:
                st.error(f"Error generating timeline: {e}")

        else:
            st.info("No data in vault yet.")

if __name__ == "__main__":
    main()