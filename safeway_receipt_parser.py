import streamlit as st
from notion_client import Client
from anthropic import Anthropic
from datetime import datetime, timedelta
import json
import base64

# --- 1. CONFIGURATION & SECRETS ---
st.set_page_config(page_title="Safeway Receipt Parser", page_icon="🛒", layout="wide")

try:
    notion = Client(auth=st.secrets["FRIDGE_NOTION_TOKEN"])
    DATABASE_ID = st.secrets["FRIDGE_NOTION_DATABASE_ID"]
    anthropic = Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
except Exception as e:
    st.error("❌ Missing Secrets! Check your Streamlit Cloud Settings.")
    st.stop()

# --- 2. HELPER FUNCTIONS ---

def parse_receipt_with_claude(pdf_bytes, added_by):
    """Send receipt PDF to Claude and extract perishable items with expiry estimates."""
    
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    
    today = datetime.now().date().isoformat()
    
    prompt = f"""You are analyzing a Safeway grocery receipt. Today's date is {today}.

Your job is to:
1. Identify ONLY perishable food items that belong in a refrigerator (produce, meat, dairy, deli, fresh items)
2. IGNORE non-perishables like canned goods, dry goods, beverages, cleaning products, spices, oils, etc.
3. For each perishable item, estimate a realistic expiry date based on typical fridge life

Return ONLY a JSON array with no other text, like this:
[
  {{
    "food": "Chicken Thighs",
    "expiry_date": "2026-02-21",
    "cost": 8.22,
    "notes": "Sanderson Farms, 6.37lb"
  }},
  ...
]

Use these typical fridge lifespans as a guide:
- Chicken/ground meat: 2 days
- Whole cuts of meat (steak, roast): 3-5 days
- Fresh fish/seafood: 1-2 days
- Eggs: 35 days
- Milk/cream: 7-10 days
- Hard cheese: 21 days
- Soft cheese/deli items: 5-7 days
- Fresh herbs: 7-10 days
- Leafy greens/sprouts: 5-7 days
- Berries/soft fruit: 5-7 days
- Hardy produce (carrots, cabbage, onions): 14-21 days
- Tomatoes: 5-7 days
- Pre-packaged deli items: 5 days

Be conservative — it's better to flag something as expiring sooner than later.
Only return the JSON array, nothing else."""

    response = anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ]
    )
    
    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    
    return json.loads(raw)

def add_to_notion(item, added_by):
    """Add a single item to the fridge Notion database."""
    try:
        notion.pages.create(
            parent={"database_id": DATABASE_ID},
            properties={
                "Food": {"title": [{"text": {"content": item["food"]}}]},
                "Date Added": {"date": {"start": datetime.now().date().isoformat()}},
                "Expires": {"date": {"start": item["expiry_date"]}},
                "Meal Cost": {"number": item.get("cost", 0.0)},
                "Added By": {"select": {"name": added_by}},
                "Notes": {"rich_text": [{"text": {"content": item.get("notes", "Via Safeway receipt")}}]},
                "Archived": {"checkbox": False}
            }
        )
        return True
    except Exception as e:
        return str(e)

# --- 3. UI ---
st.title("🛒 Safeway Receipt Parser")
st.write("Upload your Safeway email receipt PDF and we'll automatically add the perishable items to your fridge tracker.")

# Step 1: Upload + Who added it
col1, col2 = st.columns(2)
with col1:
    pdf_file = st.file_uploader("📄 Upload Safeway Receipt PDF", type=["pdf"])
with col2:
    added_by = st.selectbox("Who went shopping?", ["You", "Wife"])

# Step 2: Parse receipt
if pdf_file and st.button("🔍 Parse Receipt", type="primary"):
    with st.spinner("Analyzing receipt with Claude..."):
        try:
            pdf_bytes = pdf_file.read()
            items = parse_receipt_with_claude(pdf_bytes, added_by)
            st.session_state["parsed_items"] = items
            st.session_state["added_by"] = added_by
        except Exception as e:
            st.error(f"Error parsing receipt: {str(e)}")

# Step 3: Show parsed items for review
if "parsed_items" in st.session_state and st.session_state["parsed_items"]:
    items = st.session_state["parsed_items"]
    
    st.divider()
    st.subheader(f"🥦 Found {len(items)} perishable items — review and adjust before saving")
    st.caption("You can edit any item before adding to your fridge tracker.")

    # Editable table
    edited_items = []
    for i, item in enumerate(items):
        with st.expander(f"**{item['food']}** — expires {item['expiry_date']}", expanded=True):
            c1, c2, c3, c4 = st.columns([2, 1.5, 1, 2])
            with c1:
                food = st.text_input("Food name", value=item["food"], key=f"food_{i}")
            with c2:
                expiry = st.date_input(
                    "Expiry date",
                    value=datetime.strptime(item["expiry_date"], "%Y-%m-%d").date(),
                    key=f"expiry_{i}"
                )
            with c3:
                cost = st.number_input("Cost ($)", value=float(item.get("cost", 0.0)), min_value=0.0, step=0.01, key=f"cost_{i}")
            with c4:
                notes = st.text_input("Notes", value=item.get("notes", ""), key=f"notes_{i}")
            
            include = st.checkbox("Include this item", value=True, key=f"include_{i}")
            
            if include:
                edited_items.append({
                    "food": food,
                    "expiry_date": expiry.isoformat(),
                    "cost": cost,
                    "notes": notes
                })

    st.divider()
    
    included_count = len(edited_items)
    if included_count > 0:
        if st.button(f"✅ Add {included_count} items to Fridge Tracker", type="primary"):
            progress = st.progress(0)
            success_count = 0
            errors = []
            
            for i, item in enumerate(edited_items):
                result = add_to_notion(item, st.session_state["added_by"])
                if result is True:
                    success_count += 1
                else:
                    errors.append(f"{item['food']}: {result}")
                progress.progress((i + 1) / len(edited_items))
            
            if success_count == len(edited_items):
                st.success(f"🎉 Successfully added {success_count} items to your fridge tracker!")
                st.balloons()
                del st.session_state["parsed_items"]
            else:
                st.warning(f"Added {success_count} of {len(edited_items)} items.")
                for err in errors:
                    st.error(err)
    else:
        st.info("No items selected — check the boxes above to include items.")
