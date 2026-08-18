import pandas as pd

import os

import numpy as np

import streamlit as st

from pathlib import Path

CREDENTIALS_FILE = Path("CREDENTIALS.csv")
REFERENCE_DIR = Path("BATCH_REFERENCE")

st.title("EMR BATCH UPDATE TOOL")

if not CREDENTIALS_FILE.is_file():
    st.error(f"Missing credentials file: {CREDENTIALS_FILE}")
    st.stop()

dfcred = pd.read_csv(CREDENTIALS_FILE)
dfcred = dfcred[dfcred["user"].notna()].copy()
for column in ("DISTRICT", "FACILITY"):
    dfcred[column] = dfcred[column].astype(str).str.strip()

district = st.radio(
    "DISTRICT",
    dfcred["DISTRICT"].unique(),
    index=None,
    horizontal=True,
)
if district is None:
    st.stop()

district_credentials = dfcred[dfcred["DISTRICT"] == district].copy()

facility = st.radio(
    "FACILITY",
    district_credentials["FACILITY"].dropna().unique(),
    index=None,
    horizontal=True,
    key=f"facility_{district}",
)
if facility is None:
    st.stop()

facility_credentials = district_credentials[
    district_credentials["FACILITY"] == facility
].copy()
if facility_credentials.empty:
    st.error("No credentials matched the selected Facility.")
    st.stop()

row = facility_credentials.iloc[0]
credential_fields = ("ip", "user", "password")
if any(
    pd.isna(row.get(name)) or not str(row.get(name)).strip()
    for name in credential_fields
):
    st.error("The selected facility has incomplete server credentials.")
    st.stop()

base_url = f"http://{str(row['ip']).strip()}:8081/openmrs"
username = str(row["user"]).strip()
password = str(row["password"]).strip()

reference_file = REFERENCE_DIR / f"{facility}.csv"
if not reference_file.is_file():
    st.error(
        f"No reference dataset was found for {facility}: {reference_file}"
    )
    st.stop()

dfref = pd.read_csv(reference_file)
if "Art" not in dfref.columns:
    st.error(f"{reference_file} is missing the required Art column.")
    st.stop()

uploaded_file = st.file_uploader("Upload enrollments CSV", type="csv")
if uploaded_file is None:
    st.stop()

df = pd.read_csv(uploaded_file)

df['Service Type'] = df['Service Type'].astype(str)

df = df[df['Service Type'].str.contains('ART')].copy()

a = df.shape[0]

df = df[['MR - First name', 'MR - Surname', 'MR - Sex' ,'HIV/ART-Next Appointment date', 'Last updated on','ART: Art Number','HIV-ART Regimen - No. of days dispensed','Service Type']]

df[['HIV/ART-Next Appointment date', 'Last updated on']] = (df[['HIV/ART-Next Appointment date', 'Last updated on']]
                                                            .apply(lambda col: pd.to_datetime(col,format='mixed',dayfirst=True).dt.date))

dfart = df[df['ART: Art Number'].isnull()].copy()

dfartn = df[df['ART: Art Number'].notnull()].copy()

dfartn['ART'] = dfartn['ART: Art Number'].astype(str).str.replace('[^0-9]', '', regex=True)

dfartn['ART'] = dfartn['ART'].fillna(0)

dfartn['ART'] = pd.to_numeric(dfartn['ART'], errors = 'coerce')

dfartna = dfartn[dfartn['ART']<1].copy()

if dfartna.shape[0]>0:
    dfart = pd.concat([dfartna, dfart])
    dfartn = dfartn[dfartn['ART']>0].copy()
    

dfartn['ART'] = dfartn['ART: Art Number'].astype(str).str.replace('[^0-9]', '', regex=True)

dfart['ART_STATUS'] = 'NO ART NUMBER'

no_art = dfart.shape[0]

dfdup = dfartn[dfartn['ART'].duplicated()].copy()

dfdup['DUP STATUS'] = 'DUPLICATED IN E-REGISTER'

dup_ereg = dfdup.shape[0]

dfnodup = dfartn[~dfartn['ART'].duplicated()].copy()

if dfdup.shape[0]>0:
    dfartn = pd.concat([dfdup, dfnodup])
else:
    dfartn = dfnodup

if dfart.shape[0]>0:
    df = pd.concat([dfart, dfartn])
else:
    df = dfartn

df['HIV-ART Regimen - No. of days dispensed'] = pd.to_numeric(df['HIV-ART Regimen - No. of days dispensed'], errors = 'coerce')

dfnopills = df[df['HIV-ART Regimen - No. of days dispensed'].isnull()].copy()#NO PILLS 

dfpills = df[df['HIV-ART Regimen - No. of days dispensed'].notnull()].copy() #HAS PILLS

dfnoday = dfnopills[((dfnopills['HIV-ART Regimen - No. of days dispensed'].isnull()) & (dfnopills['HIV/ART-Next Appointment date'].isnull()))].copy()

dfday = dfnopills[((dfnopills['HIV-ART Regimen - No. of days dispensed'].isnull()) & (dfnopills['HIV/ART-Next Appointment date'].notnull()))].copy()

dfday = dfday.drop(columns=['HIV-ART Regimen - No. of days dispensed'])

dfday[['HIV/ART-Next Appointment date', 'Last updated on']] = (dfday[['HIV/ART-Next Appointment date', 'Last updated on']]
                                                            .apply(lambda col: pd.to_datetime(col,format='mixed',dayfirst=True)))

dfday['HIV-ART Regimen - No. of days dispensed'] = (
    pd.to_datetime(
        dfday['HIV/ART-Next Appointment date'],
        errors='coerce',
    )
    - pd.to_datetime(
        dfday['Last updated on'],
        errors='coerce',
    )
) / pd.Timedelta(days=1)

dfnoday['DAYS_STATUS'] = 'MISSING DAYS DISPENSED'

dfnoday = dfnoday.drop(columns =['HIV/ART-Next Appointment date','HIV-ART Regimen - No. of days dispensed'])

#dfa = pd.concat([dfday, dfnoday])

dfs = [dfx for dfx in [dfday, dfnoday] if not df.empty]


dfa = pd.concat(dfs, ignore_index=True)

dfnodate = dfpills[dfpills['HIV/ART-Next Appointment date'].isnull()].copy()

dfdate = dfpills[dfpills['HIV/ART-Next Appointment date'].notna()].copy()

dfnodate = dfnodate.drop(columns =['HIV/ART-Next Appointment date'])

dfnodate['Last updated on'] = pd.to_datetime(dfnodate['Last updated on'],format='mixed',dayfirst=True)

dfnodate['HIV-ART Regimen - No. of days dispensed'] = pd.to_numeric(dfnodate['HIV-ART Regimen - No. of days dispensed'], errors='coerce')

dfnodate['days'] = pd.to_timedelta(dfnodate['HIV-ART Regimen - No. of days dispensed'],unit='D')

dfnodate['HIV/ART-Next Appointment date'] = dfnodate['Last updated on'] + dfnodate['days']

#dfb = pd.concat([dfdate, dfnodate])

dfs = [dfx for dfx in [dfdate, dfnodate] if not df.empty]


dfb = pd.concat(dfs, ignore_index=True)

#df = pd.concat([dfa, dfb])

dfs = [dfx for dfx in [dfa, dfb] if not df.empty]


df = pd.concat(dfs, ignore_index=True)

def pillcheck(days):
    if pd.isna(days):
        return None
    if days < 0:
        return 'NEXT APPT < LAST ENCOUNTER, CHECK'
    if 0 <= days < 30:
        return 'FEW DAYS DISPENSED, CHECK'
    if 30 <= days <= 185:
        return None
    if days > 185:
        return 'MANY DAYS DISPENSED, CHECK'
    return None

df['HIV-ART Regimen - No. of days dispensed'] = pd.to_numeric(df['HIV-ART Regimen - No. of days dispensed'], errors='coerce').copy()

df['DAYS ERROR']  = df['HIV-ART Regimen - No. of days dispensed'].apply(pillcheck)

dfmany = df[df['DAYS ERROR']=='MANY DAYS DISPENSED, CHECK'].copy()

dfew = df[df['DAYS ERROR']== 'FEW DAYS DISPENSED, CHECK'].copy()
dfqn = df[df['DAYS ERROR']== 'NEXT APPT < LAST ENCOUNTER, CHECK'].copy()
dfcorrect = df[~df['DAYS ERROR'].isin(['MANY DAYS DISPENSED, CHECK','FEW DAYS DISPENSED, CHECK', 'NEXT APPT < LAST ENCOUNTER, CHECK'])].copy()

df = pd.concat(
    [dfew, dfmany, dfqn, dfcorrect],
    ignore_index=True,
)
df = df[['MR - First name', 'MR - Surname', 'MR - Sex','Service Type','ART: Art Number','ART', 
         'Last updated on','HIV-ART Regimen - No. of days dispensed', 'HIV/ART-Next Appointment date','ART_STATUS',  
         'DUP STATUS','DAYS_STATUS', 'days','DAYS ERROR']].copy()
for date_column in (
    'Last updated on',
    'HIV/ART-Next Appointment date',
):
    df[date_column] = pd.to_datetime(
        df[date_column],
        format='mixed',
        dayfirst=True,
        errors='coerce',
    ).dt.strftime('%d/%m/%Y')

checkd = {'NO ART NOs': dfart.shape[0],
          'DUPLICATED IN E-REG': dfdup.shape[0],
          'NEXT APPT < LAST ENCOUNTER, CHECK': dfqn.shape[0],
          'NO DAYS DISPENSED' : dfnoday.shape[0],
          'FEW DAYS DISPENSED' : dfew.shape[0],
          'TOO MANY DAYS DISPENSED': dfmany.shape[0]
}

has_data_issues = False
for key,value in checkd.items():
    if value>0:
        st.write(f'{key}: {value}')
        has_data_issues = True

if has_data_issues:
    st.download_button(
        "CLEAN DATA",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="clean_data.csv",
        mime="text/csv",
    )
    st.stop()

b =df.shape[0] + dfmany.shape[0] + dfew.shape[0]+dfqn.shape[0] 



df.shape

if a-b !=0:
    st.warning('warning')
else:
    st.info('NO ERRORS FOUND')

df = df[['MR - First name', 'MR - Surname', 'MR - Sex' ,'HIV/ART-Next Appointment date', 'Last updated on','ART: Art Number','HIV-ART Regimen - No. of days dispensed']]

df = df.rename(columns = {'HIV/ART-Next Appointment date':'Return Visit Date', 'Last updated on':'Last Encounter Date',
                          'HIV-ART Regimen - No. of days dispensed': 'Days Dispensed'})

df[['Return Visit Date','Last Encounter Date']] = df[['Return Visit Date','Last Encounter Date']].apply(lambda col: pd.to_datetime(col,format='mixed',dayfirst=True))

df['Rday'] = df['Return Visit Date'].dt.day

df['Rmonth'] = df['Return Visit Date'].dt.month

df['Ryear'] = df['Return Visit Date'].dt.year

df['Lday'] = df['Last Encounter Date'].dt.day

df['Lmonth'] = df['Last Encounter Date'].dt.month

df['Lyear'] = df['Last Encounter Date'].dt.year

df['ART'] = df['ART: Art Number'].astype(str).str.replace('[^0-9]', "", regex= True)

df2 = dfref.copy()

df2['ART'] = df2['Art'].astype(str).str.replace('[^0-9]', "", regex= True)

df2['ARVS'] = df2['ARVS'].astype(str).str.replace('/', '-')

df2['ART'] = pd.to_numeric(df2['ART'], errors = 'coerce')

df['ART'] = pd.to_numeric(df['ART'], errors = 'coerce')

df2 = df2[df2['ART'].notna()].copy()

df = df[df['ART'].notna()].copy()

df['ART'] = pd.to_numeric(df['ART'], errors = 'coerce')

df = df.drop_duplicates(subset= ['ART'], keep='first') ####DUPS WON'T PASS ANYWAY, REMOVE LATER

df['ART'] = pd.to_numeric(df['ART'], errors = 'coerce')

df2['ART'] = pd.to_numeric(df2['ART'], errors = 'coerce')

df = pd.merge(df, df2, on ='ART', how = 'left')

dfdupe =  df[df['ART'].duplicated()].copy()

df =  df[~df['ART'].duplicated()].copy()

dfdup['REASON_REJECTED'] = 'DUPLICATED IN EMR, UPDATE ONE BY ONE'

dfnoart = df[df['Art'].isnull()].copy()

df = df[df['Art'].notnull()].copy()

dfnoart['REASON_REJECTED'] = 'NOT IN EMR, MAY BE TX_NEWS/VISITORS'

file2 = r"C:\Users\Desire Luminsa\Desktop\CV"

out = os.path.join(file2, 'ALLS.csv')

df = df[['Art', 'Days Dispensed', 'Rday','Rmonth', 'Ryear', 'Lday', 'Lmonth', 'Lyear', 'ART', 'Art', 'ARVS']].copy()
