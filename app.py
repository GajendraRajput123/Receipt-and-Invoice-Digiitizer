import streamlit as st
import pandas as pd
import sqlite3
import plotly.express as px
from PIL import Image, ImageOps
import pytesseract
import re
from datetime import datetime
from pdf2image import convert_from_bytes

DB_NAME = 'receipt_vault1.db'

# --- DATABASE FUNCTIONS ---
def init_db():
    """Initializes the SQLite database with necessary tables."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Added tax_amount to the schema
    c.execute('''CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    merchant TEXT,
                    date TEXT,
                    total_amount REAL,
                    tax_amount REAL,
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

def check_if_receipt_exists(merchant, date, total):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id FROM receipts WHERE merchant = ? AND date = ? AND total_amount = ?", 
              (merchant, date, total))
    data = c.fetchone()
    conn.close()
    return data is not None

def save_receipt_to_db(merchant, date, total, tax, filename, line_items_data):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Insert with Tax
    c.execute("INSERT INTO receipts (merchant, date, total_amount, tax_amount, filename, upload_timestamp) VALUES (?, ?, ?, ?, ?, ?)",
              (merchant, date, total, tax, filename, upload_time))
    receipt_id = c.lastrowid
    for item in line_items_data:
        c.execute("INSERT INTO line_items (receipt_id, name, qty, price) VALUES (?, ?, ?, ?)",
                  (receipt_id, item['name'], item['qty'], item['price']))
    conn.commit()
    conn.close()
    return True

def get_all_receipts():
    conn = sqlite3.connect(DB_NAME)
    # Fetch tax as well
    df = pd.read_sql_query("SELECT id, merchant, date, total_amount AS total, tax_amount as tax FROM receipts ORDER BY id DESC", conn)
    conn.close()
    return df

def get_detailed_bill_data(receipt_id):
    conn = sqlite3.connect(DB_NAME)
    query = """
        SELECT 
            r.id as "Bill ID",
            r.merchant as "Vendor Name",
            l.name as "Item Name",
            l.qty as "Quantity",
            l.price as "Unit Price"
        FROM line_items l
        JOIN receipts r ON l.receipt_id = r.id
        WHERE r.id = ?
    """
    df = pd.read_sql_query(query, conn, params=(receipt_id,))
    conn.close()
    return df

def get_receipt_metadata(receipt_id):
    """Helper to get totals and tax for a specific ID for validation"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT total_amount, tax_amount FROM receipts WHERE id = ?", (receipt_id,))
    data = c.fetchone()
    conn.close()
    return data if data else (0.0, 0.0)

def clear_database():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DROP TABLE IF EXISTS line_items")
    c.execute("DROP TABLE IF EXISTS receipts")
    conn.commit()
    conn.close()
    init_db() # Re-init immediately

# --- PROCESSING & PARSING FUNCTIONS ---
def preprocess_image(image):
    return ImageOps.grayscale(image)

def extract_text(image):
    return pytesseract.image_to_string(image)

def parse_receipt_data(text):
    data = {"merchant": "Unknown", "date": None, "total": 0.0, "tax": 0.0}
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if lines:
        data["merchant"] = lines[0]

    date_match = re.search(r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})', text)
    if date_match:
        data["date"] = date_match.group(0)
    else:
        data["date"] = datetime.now().strftime("%Y-%m-%d")

    # Extract Total
    amount_match = re.findall(r'(?i)(?:Total|Amount|Due|Balance|Grand Total)[\s:$]*([\d,]+\.\d{2})', text)
    if amount_match:
        try:
            # Usually the last amount found is the total
            data["total"] = float(amount_match[-1].replace(',', ''))
        except:
            pass

    # Extract Tax (New Logic)
    # Looks for Tax, VAT, GST followed by a number
    tax_match = re.findall(r'(?i)(?:Tax|VAT|GST|Sales\s*Tax)[\s:$]*([\d,]+\.\d{2})', text)
    if tax_match:
        try:
            # We take the largest value found associated with tax, or the last one. 
            # Often receipts have sub-taxes, but usually the summary line is near the bottom.
            taxes = [float(t.replace(',', '')) for t in tax_match]
            data["tax"] = max(taxes) if taxes else 0.0
        except:
            pass
            
    return data

def parse_line_items_data(text):
    items = []
    # Regex checks for price at end of line
    price_pattern = r'[\s$]([\d,]+\.\d{2})\s*$'
    
    for line in text.split('\n'):
        line = line.strip()
        price_match = re.search(price_pattern, line)
        if price_match:
            price_str = price_match.group(1)
            try:
                price = float(price_str.replace(',', ''))
                name_part = line[:price_match.start()].strip()
                
                # Basic logic to skip lines that are likely totals or dates
                if any(x in name_part.lower() for x in ['total', 'amount', 'due', 'visa', 'mastercard', 'cash', 'change', 'tax']):
                    continue

                qty = 1
                # Check for "2 x Burger" format
                qty_match = re.match(r'^(\d+)\s*[xX]\s*', name_part)
                if qty_match:
                    qty = int(qty_match.group(1))
                    name = name_part[qty_match.end():].strip()
                else:
                    name = name_part
                
                if name:
                    items.append({"name": name, "qty": qty, "price": price})
            except ValueError:
                continue
    return items

# --- MAIN APP ---
def main():
    st.set_page_config(page_title="Receipt Vault & Analyzer", layout="wide", page_icon="💾")
    init_db()

    with st.sidebar:
        st.header("🔑 Authentication")
        api_key = st.text_input("General API Key", type="password") 
        st.divider()
        st.warning("Database Schema Updated. If you see errors, clear records.")
        if st.button("Clear All Records"):
            clear_database()
            st.toast("All records and tables reset!", icon="🗑️")
            st.rerun()

    col_header_1, col_header_2 = st.columns([1, 15])
    with col_header_1:
        st.image("https://cdn-icons-png.flaticon.com/512/287/287221.png", width=50)
    with col_header_2:
        st.title("Receipt Vault & Analyzer")

    tab_vault, tab_analytics = st.tabs(["🩸 Vault & Upload", "📊 Analytics Dashboard"])

    # === TAB 1: VAULT & UPLOAD ===
    with tab_vault:
        col_upload, col_storage = st.columns([2, 3])
        
        with col_upload:
            st.subheader("Upload Document")
            uploaded_file = st.file_uploader("Upload Receipt (JPG/PNG/PDF)", type=["png", "jpg", "jpeg", "pdf"])
            
            if uploaded_file:
                st.subheader("Image Processing")
                original_image = None
                
                if uploaded_file.type == "application/pdf":
                    try:
                        images = convert_from_bytes(uploaded_file.read())
                        if images:
                            original_image = images[0]
                            st.info("PDF detected: Processed first page as image.")
                        else:
                            st.error("Could not convert PDF.")
                            st.stop()
                    except Exception as e:
                        st.error(f"Error converting PDF: {e}")
                        st.stop()
                else:
                    original_image = Image.open(uploaded_file)

                if original_image:
                    cleaned_image = preprocess_image(original_image)
                    st.image(cleaned_image, caption="Processed Image", use_container_width=True)
                    
                    if st.button("🚀 Process & Save to Vault", type="primary", use_container_width=True):
                        with st.spinner(" performing OCR and extracting data..."):
                            raw_text = extract_text(cleaned_image)
                            receipt_data = parse_receipt_data(raw_text)
                            line_items = parse_line_items_data(raw_text)
                            
                            is_duplicate = check_if_receipt_exists(
                                receipt_data['merchant'], receipt_data['date'], receipt_data['total']
                            )

                            if is_duplicate:
                                st.error(f"Duplicate Receipt Detected from {receipt_data['merchant']}!")
                            else:
                                if save_receipt_to_db(receipt_data['merchant'], receipt_data['date'], receipt_data['total'], receipt_data['tax'], uploaded_file.name, line_items):
                                    st.success("Receipt processed and saved successfully!")
                                    st.metric("Extracted Tax", f"${receipt_data['tax']:.2f}")
                                    st.rerun() 

        with col_storage:
            st.subheader("Persistent Storage")
            receipts_df = get_all_receipts()
            st.dataframe(receipts_df, use_container_width=True, hide_index=True)
            
            st.divider()
            
            # --- MODIFIED SECTION: DETAILED BILL ITEMS ---
            st.subheader("🔍 Detailed Bill Items & Validation")
            if not receipts_df.empty:
                receipt_ids = receipts_df['id'].tolist()
                selected_id = st.selectbox("Select ID to view items:", receipt_ids)
                
                if selected_id:
                    items_df = get_detailed_bill_data(selected_id)
                    official_total, official_tax = get_receipt_metadata(selected_id)
                    
                    if not items_df.empty:
                        items_df.insert(0, 'S.No', range(1, 1 + len(items_df)))
                        items_df['Total Price'] = items_df['Quantity'] * items_df['Unit Price']
                        st.dataframe(items_df, use_container_width=True, hide_index=True)
                        
                        # --- VALIDATION LOGIC ---
                        st.markdown("### 🧾 Mathematical Validation")
                        st.caption("Formula: Subtotal (Sum of Items) + Tax = Official Total")
                        
                        calculated_subtotal = items_df['Total Price'].sum()
                        calculated_grand_total = calculated_subtotal + official_tax
                        discrepancy = official_total - calculated_grand_total
                        
                        # Visual Metrics
                        c1, c2, c3, c4, c5 = st.columns(5)
                        c1.metric("Sum(Items)", f"${calculated_subtotal:,.2f}")
                        c2.markdown("<h3 style='text-align: center;'>+</h3>", unsafe_allow_html=True)
                        c3.metric("Extracted Tax", f"${official_tax:,.2f}")
                        c4.markdown("<h3 style='text-align: center;'>=</h3>", unsafe_allow_html=True)
                        c5.metric("Calc. Total", f"${calculated_grand_total:,.2f}")

                        st.divider()
                        
                        final_c1, final_c2 = st.columns(2)
                        final_c1.metric("Official Receipt Total", f"${official_total:,.2f}")
                        final_c2.metric("Discrepancy", f"${discrepancy:,.2f}", 
                                        delta_color="normal" if abs(discrepancy) < 0.1 else "inverse")

                        if abs(discrepancy) < 0.1:
                            st.success("✅ VALID: The line items plus tax match the total amount.")
                        else:
                            st.warning("⚠️ INVALID: Mismatch detected. Possible reasons:")
                            st.markdown("""
                            * OCR missed a line item.
                            * Tax was not extracted correctly (try manual edit if implemented).
                            * The receipt contains a Tip or Service Charge not accounted for.
                            """)
                    else:
                        st.warning("No line items extracted. Only the Total and Tax header data was found.")
            else:
                st.info("Upload a receipt to see line items.")

    # === TAB 2: ANALYTICS DASHBOARD ===
    with tab_analytics:
        st.subheader("📊 Spending Insights")
        df = get_all_receipts()
        
        if not df.empty:
            df['total'] = pd.to_numeric(df['total'], errors='coerce').fillna(0)
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            
            col_charts_1, col_charts_2 = st.columns(2)
            with col_charts_1:
                st.markdown("**Spending by Merchant**")
                fig_pie = px.pie(df, values='total', names='merchant', hole=0.4)
                st.plotly_chart(fig_pie, use_container_width=True)
            with col_charts_2:
                st.markdown("**Tax vs Total Ratio**")
                # Simple scatter to see if tax correlates with total
                fig_scat = px.scatter(df, x='total', y='tax', color='merchant', 
                                      labels={'total': 'Bill Total', 'tax': 'Tax Amount'})
                st.plotly_chart(fig_scat, use_container_width=True)

            st.subheader("Spending Over Time")
            daily_spend = df.groupby('date')['total'].sum().reset_index()
            daily_spend = daily_spend.sort_values('date')
            fig_line = px.line(daily_spend, x='date', y='total', markers=True)
            st.plotly_chart(fig_line, use_container_width=True)
        else:
            st.info("No data available for analysis.")

if __name__ == "__main__":
    main()