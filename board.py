import streamlit as st

import glob
import streamlit as st 
import pandas as pd
import os
import random


def dashboard():
    st.title("INTRODUCTION DASHBOARD")


pages = {
    "DASHBOARD:": [
        st.Page(dashboard, title="INTRODUCTION DASHBOARD"),
    ],
    "LINELISTS:":[
        st.Page("batch3.py", title="E-REGISTERS"),
        st.Page("batch4.py", title="EMR BATCH UPLOAD"),
        st.Page("batch5.py", title="BTACH EMR"),
        st.Page("batch2.py", title='ONE BY ONE')
    ],
}

pg = st.navigation(pages)
pg.run()
                                
    
