from uuid import uuid4
import json
import pytest
from fastapi import HTTPException
from app.services.channel_connection_bridge import convert_channel_packet


def message(kind,payload=None):
    return {'id':str(uuid4()),'messageType':kind,'text':'Teams 고정 답변','participantKind':'bot','participantId':'bot','payload':{'sourceTalkNodeId':'teams-talk',**(payload or {})}}


@pytest.mark.parametrize('kind,payload',[
 ('text',{}),('button',{'options':['확인','다음']}),('link-button',{'options':[{'label':'링크','url':'https://example.com/'}]}),
 ('card',{'card':{'title':'제목','description':'본문','imageUrl':'https://example.com/image.png'}}),
 ('carousel',{'carousel':{'title':'목록','itemTitle':'항목','itemContents':'내용','itemButtonLabel':'선택','itemButtonValue':'A','buttonType':'Button'}}),
 ('table',{'columns':['name','token'],'rows':[{'name':'행','token':'답변 열 보존'}],'keyColumn':'name','selectable':True}),
 ('form-a-card',{'adaptiveCard':{'type':'AdaptiveCard','version':'1.5','body':[{'type':'Input.Text','id':'name'}],'actions':[{'type':'Action.Submit','title':'확인','data':{'kept':'원문'}}]}})
])
def test_teams_packets_use_existing_adaptive_normalizer_and_keep_output(kind,payload):
    source=message(kind,payload);packet=convert_channel_packet('ms-teams','replies',{'messages':[source],'destination':{}})[0]
    assert packet['type']=='message' and 'Teams 고정 답변' in json.dumps(packet,ensure_ascii=False)
    if kind!='text':assert packet['attachments'][0]['contentType']=='application/vnd.microsoft.card.adaptive'
    if kind=='table':assert '답변 열 보존' in json.dumps(packet,ensure_ascii=False)
    if kind in {'button','form-a-card'}:
        data=packet['attachments'][0]['content']['actions'][0]['data']
        assert data['sourceMessageId']==source['id'] and data['sourceTalkNodeId']=='teams-talk' and data['webchatRichFormVersion']=='1.0'
        if kind=='form-a-card':assert data['kept']=='원문'


def test_teams_packet_rejects_unsafe_links_before_any_delivery():
    source=message('link-button',{'options':[{'label':'unsafe','url':'javascript:alert(1)'}]})
    with pytest.raises(HTTPException):convert_channel_packet('ms-teams','replies',{'messages':[source],'destination':{}})


def test_teams_adaptive_inputs_return_standard_rich_form_response():
    from app.services.channel_connection_teams import TeamsAdapter
    from tests.test_channel_connection_teams_adapter import activity
    tenant=str(uuid4());client=str(uuid4())
    source=message('form-a-card',{'adaptiveCard':{'type':'AdaptiveCard','version':'1.6','body':[{'type':'Input.Text','id':'name'}],'actions':[{'type':'Action.Submit','title':'제출','data':{'kept':'보존'}}]}})
    packet=convert_channel_packet('ms-teams','replies',{'messages':[source],'destination':{}})[0]
    value={**packet['attachments'][0]['content']['actions'][0]['data'],'name':'입력값'}
    body=activity(tenant,client);body.update(text='',value=value,entities=[])
    event=TeamsAdapter._event(body,{'tenant':tenant},body['conversation']['id'],body['from']['id'],body['recipient']['id'])
    wire=json.loads(event['message'])
    assert wire['response']['name']=='입력값' and wire['response']['kept']=='보존'
    assert wire['sourceMessageId']==source['id'] and event['addressed']
