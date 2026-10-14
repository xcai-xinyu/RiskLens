"""RiskLens Streamlit presentation. All research/history operations use HTTP API."""
from datetime import date

import streamlit as st

from frontend.client import APIClient, APIError

CATEGORIES=['liquidity','credit','market','operational','regulatory']
RISK_COLOR={'LOW':'#587266','MEDIUM':'#a36c18','HIGH':'#b94343','UNKNOWN':'#647180'}


def risk_badge(level):
    # Only schema-enumerated levels enter this constant markup; source text uses native widgets.
    color=RISK_COLOR.get(level,RISK_COLOR['UNKNOWN'])
    st.markdown(f'<span style="color:{color};font-weight:650">{level if level in RISK_COLOR else "UNKNOWN"}</span>',unsafe_allow_html=True)


def render_report(report):
    metadata=report['metadata']
    st.subheader('Risk Overview')
    st.caption(f"{report['company']} · As of {report['as_of_date']} · {metadata.get('label',metadata.get('mode','Unknown mode'))}")
    if metadata.get('simulated') or metadata.get('mode')=='demo':
        st.info('SIMULATED semantic verification. This report does not validate a live provider.')
    cols=st.columns(3)
    with cols[0]:
        st.caption('Overall risk');risk_badge(report['overall_risk'])
    cols[1].metric('Report status',report['status'])
    coverage=metadata.get('claim_coverage',{})
    cols[2].metric('Verified claims',f"{coverage.get('supported_final',len(report['claims']))} / {coverage.get('original','—')}")
    st.write(report['summary'] or 'No verified summary is available.')
    st.caption(f"{len(report['gaps'])} gaps · UNKNOWN means insufficient evidence, distinct from LOW. HIGH describes risk, not a failed research task.")
    st.subheader('Risk Factors')
    factors={f['category']:f for f in report['risk_factors']}
    for category in CATEGORIES:
        factor=factors.get(category)
        with st.expander(category.title(),expanded=False):
            risk_badge(factor['level'] if factor else 'UNKNOWN')
            if not factor:
                st.write('Category was not researched.');continue
            st.write(factor['rationale'])
            st.caption('Verified claim IDs: '+(', '.join(factor['verified_claim_ids']) or 'None'))
            st.write('Missing data:',factor['missing_data'] or ['None recorded'])
    st.subheader('Claims & Evidence')
    decisions={v['claim_id']:v for v in metadata.get('verification_results',[])}
    citations=metadata.get('citations',{})
    sources={s['document_id']:s for s in report['sources']}
    for claim in report['claims']:
        decision=decisions.get(claim['claim_id'],{})
        st.write(claim['text'])
        st.caption(f"{claim['claim_id']} · {claim['claim_type'].upper()} · verification: {decision.get('status','unknown')} · evidence support score (0–100; not probability): {decision.get('evidence_support_score','—')}")
        st.caption('Metric IDs: '+(', '.join(claim['metric_ids']) or 'None'))
        ids=set(claim['evidence_ids'])
        ids.update(i['evidence_id'] for m in report['metrics'] if m['metric_id'] in claim['metric_ids'] for i in m['inputs'])
        for eid in sorted(ids):
            citation=citations.get(eid)
            with st.expander(f'Citation · {eid}'):
                if not citation:
                    st.write('Citation not present in the stored snapshot.');continue
                source=sources.get(citation['document_id'],{})
                st.write(source.get('title','Source title unavailable'))
                st.caption(f"Published {source.get('published_at','Unknown')} · page {citation.get('page')} · section {citation.get('section')}")
                st.text(citation['quote'])
                st.caption(f"Document {citation['document_id']} · chunk {citation['chunk_id']}")
                st.text(source.get('url') or source.get('local_path') or '')
    if not report['claims']:
        st.write('No verified claims. Rejected claims are retained separately below.')
    st.subheader('Metrics')
    for metric in report['metrics']:
        with st.expander(f"Calculation · {metric['name']} · {metric['value'] if metric['value'] is not None else 'Unavailable'}"):
            st.caption(f"{metric['metric_id']} · {metric['status']}")
            st.code(metric['formula'],language=None)
            for item in metric['inputs']:
                st.text(f"{item['name']} = {item['value']} {item['unit']} | currency={item['currency']} | period={item['period']} | evidence={item['evidence_id']} | {item['extraction_status']}")
            st.write('Limitations:',metric['limitations'] or ['None recorded'])
    st.subheader('Gaps & Conflicts')
    for gap in report['gaps']:
        with st.expander(f"Gap · {gap['code']}"):
            st.text(gap['message'])
    for decision in metadata.get('verification_results',[]):
        if decision['status']!='supported' or decision['conflicts']:
            with st.expander(f"Excluded / conflict · {decision['claim_id']} · {decision['status']}"):
                st.text(decision['explanation']);st.write('Conflicts:',decision['conflicts'])
                st.json(decision['checks'])
    with st.expander('Audit detail · versions, lineage and budgets'):
        st.json(metadata)


def render_compare(comparison):
    st.subheader('Compare view')
    if not comparison['directly_comparable']:
        st.warning(comparison['notice'])
    else:
        st.info(comparison['notice'])
    for column,side in zip(st.columns(2),('left','right'),strict=True):
        with column:
            item=comparison[side]
            st.write(item['company']);st.caption(item['run_id'])
            for key in ('as_of_date','status','overall_risk','mode','simulated','corpus_version','corpus_hash','model','provider','prompt_version','rubric_version'):
                st.text(f'{key}: {item.get(key)}')
            with st.expander('Source coverage and version hashes'):
                st.json(item)
    with st.expander('Observed metrics and risk factors · no inferred trend'):
        st.json({'metrics':comparison.get('observed_metrics',{}),
                 'risk_factors':comparison.get('observed_risk_factors',{})})
    st.write('Comparability reasons:',comparison['reasons'] or ['None recorded'])
    st.caption('No automated risk increase/decrease conclusion is produced.')


def render_evaluation(client):
    st.subheader('Evaluation')
    st.caption('P4 frozen paired arms · independent reference scoring · human H4 review pending')
    items=client.call('GET','/evaluations')['items']
    if not items:
        st.info('No executed evaluation artifacts available. Real-model quality remains BLOCKED.');return
    ids=[item['evaluation_id'] for item in items]
    selected=st.selectbox('Executed evaluation',ids)
    result=client.call('GET',f'/evaluations/{selected}')
    st.warning(result['label']+' · '+result['annotation_status']+' · real-model quality: '+result['real_model_quality_status'])
    st.write('No model-quality improvement is claimed. Demo/synthetic measurements validate engineering only.')
    rows=[]
    for arm,aggregate in result['aggregate'].items():
        row={'Arm':arm,'Cases':aggregate['cases'],'Failed':aggregate['failed']}
        for name in ('citation_correctness','unsupported_rate','numerical_accuracy','completeness'):
            metric=aggregate['metrics'].get(name,{})
            row[name]=metric.get('value')
        rows.append(row)
    st.dataframe(rows,hide_index=True,width='stretch')
    with st.expander('Numerators / denominators, latency, calls and unknown cost'):
        st.json(result['aggregate'])
    with st.expander('Protocol, split coverage, versions and limitations'):
        st.json({'protocol':result['protocol'],'protocol_hash':result['protocol_hash'],
                 'split_aggregate':result['split_aggregate'],'limitations':result['limitations']})
    with st.expander('Per-case outputs, failures and disagreements'):
        st.json(result['records'])


@st.fragment(run_every='2s')
def progress(client):
    run_id=st.session_state.get('active_run')
    if not run_id:
        return
    run=client.call('GET',f'/runs/{run_id}')
    st.subheader('Research progress')
    st.write(f"Task: {run['status']} · stage: {run['stage']}")
    st.caption(run_id)
    with st.expander('Run budget / calls'):
        st.json(run['budget'])
    if run['status']=='failed':
        st.error('Research execution failed. This is not a HIGH risk assessment.')
        st.write(run['errors'])
    if run['report_available']:
        render_report(client.call('GET',f'/reports/{run_id}'))
        for fmt in ('json','markdown'):
            if st.button(f'Prepare {fmt} export',key='export_'+fmt):
                data=client.call('GET',f'/reports/{run_id}/export',params={'format':fmt},export=True)
                st.download_button(f'Download {fmt}',data,file_name=f'{run_id}.{ "json" if fmt=="json" else "md"}',key='download_'+fmt)
    elif run['status'] in ('queued','running'):
        st.info('Waiting for the bounded research workflow. Refreshes every 2 seconds while this page is active.')


def main():
    st.set_page_config(page_title='RiskLens | Research terminal',page_icon='▥',layout='wide')
    st.markdown('''<style>.block-container{max-width:1180px;padding-top:2rem}h1{letter-spacing:-.035em}h2,h3{letter-spacing:-.015em}[data-testid="stMetric"]{border:1px solid #d9dfe5;border-radius:6px;padding:12px}[data-testid="stExpander"]{border-radius:6px}footer{visibility:hidden}</style>''',unsafe_allow_html=True)
    st.title('RiskLens')
    st.caption('FINANCIAL RESEARCH TERMINAL · Evidence first. Deterministic calculations. Traceable conclusions.')
    client=APIClient()
    page=st.sidebar.radio('Workspace',['Research','Report History','Evaluation'])
    st.sidebar.caption('P3 · single instance · live semantic provider remains BLOCKED unless separately configured and validated')
    try:
        if page=='Evaluation':
            render_evaluation(client);return
        if page=='Research':
            with st.form('research'):
                left,right=st.columns(2)
                company=left.text_input('Company',value='Coinbase')
                cutoff=right.date_input('As of date',value=date(2025,2,13))
                question=st.text_area('Research question',value='Assess liquidity and regulatory risks with evidence.')
                categories=st.multiselect('Risk categories',CATEGORIES,default=['liquidity','regulatory'])
                mode=st.selectbox('Mode',['demo','offline_research','live'])
                st.caption('Demo uses simulated model responses. Offline research requires configured semantic provider; live additionally requires configured discovery. No silent fallback.')
                submitted=st.form_submit_button('Run Research',type='primary')
            if submitted:
                payload={'company':company,'question':question,'as_of_date':cutoff.isoformat(),'risk_categories':categories,'mode':mode}
                if not company.strip() or not question.strip() or not categories:
                    st.error('Enter a company, question and at least one risk category.')
                else:
                    response=client.call('POST','/research',data=payload)
                    st.session_state['active_run']=response['run_id']
            progress(client)
        else:
            st.subheader('Report History')
            company=st.text_input('Filter company',value='')
            page_number=st.number_input('History page',min_value=1,value=1,step=1)
            history=client.call('GET','/reports',params={'company':company or None,'limit':20,'offset':(page_number-1)*20})
            st.caption(f"{history['total']} persisted reports · 20 per page")
            items=history['items']
            if not items:
                st.info('No reports for this filter.');return
            st.dataframe([{k:v for k,v in i.items() if k!='run_id'} for i in items],hide_index=True,width='stretch')
            ids=[i['run_id'] for i in items]
            labels={i['run_id']:f"{i['company']} · {i['as_of_date']} · {i['status']} · {i['overall_risk']} · {i['run_id'][:8]}" for i in items}
            selected=st.selectbox('Open stored report',ids,format_func=lambda i:labels[i])
            render_report(client.call('GET',f'/reports/{selected}'))
            pair=st.multiselect('Select two reports to compare',ids,format_func=lambda i:labels[i],max_selections=2)
            if st.button('Compare reports',disabled=len(pair)!=2):
                st.session_state['comparison']=client.call('POST','/reports/compare',data={'run_ids':pair})
            if st.session_state.get('comparison'):
                render_compare(st.session_state['comparison'])
    except APIError:
        st.error('API request failed. Check the local API service, task capacity and selected mode. Research failure is separate from financial risk.')


if __name__=='__main__':
    main()
