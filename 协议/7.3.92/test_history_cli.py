"""Offline regressions for current and legacy history response formats."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from history.models import (
    RunRecordDetail,
    format_pace,
    format_scaled_speed,
    history_record_status,
    parse_history_records,
)

class HistoryCliTests(unittest.TestCase):
    def test_list_and_legacy_envelope(self):
        rows=[{'rrid':1,'totalDis':2150,'totalTime':760,'status':1,'complete':True}]
        for data in (rows,{'recordList':rows},json.dumps(rows),json.dumps({'recordList':rows})):
            self.assertEqual(parse_history_records(data),rows)
        for data in (None,[],{'recordList':None},{'recordList':[]}):
            self.assertEqual(parse_history_records(data),[])

    def test_malformed_response_is_not_silently_empty(self):
        for data in ({},1,{'recordList':{}},['invalid'],'{broken'):
            with self.assertRaises(ValueError):parse_history_records(data)

    def test_status_semantics_match_official_app(self):
        self.assertEqual(history_record_status({'status':0,'complete':True}),'达标')
        self.assertEqual(history_record_status({'status':0,'complete':False}),'未达标')
        self.assertEqual(history_record_status({'status':1,'complete':True}),'未达标')
        self.assertEqual(history_record_status({'status':1,'complete':False}),'未达标')
        self.assertEqual(history_record_status({'status':2,'complete':True}),'不达标，存在异常')
        self.assertNotEqual(history_record_status({'status':1}),'达标')

    def test_record_validity_uses_status_zero_and_reason_list(self):
        base = {
            'rrid':1,'totalDis':3000,'totalTime':1060,'complete':True,
            'status':0,'unCompleteReason':0,
            'reasonList':[{'completeStatus':1},{'completeStatus':1}],
        }
        self.assertTrue(RunRecordDetail.from_api_data(base).is_valid)
        self.assertFalse(RunRecordDetail.from_api_data(dict(base,status=1)).is_valid)
        self.assertFalse(RunRecordDetail.from_api_data(
            dict(base,reasonList=[{'completeStatus':1},{'completeStatus':0}])).is_valid)

    def test_pace_formatting(self):
        self.assertEqual(format_pace(5.89), "5'53\"")
        self.assertEqual(format_pace(2.83), "2'50\"")
        self.assertEqual(format_scaled_speed(5890), "5'53\"")
        self.assertEqual(format_scaled_speed(2830), "2'50\"")
        self.assertEqual(format_pace('-'), '-')

    def test_history_cli_formats_rows_and_handles_bad_payload(self):
        spec=importlib.util.spec_from_file_location('history_test_cli',Path(__file__).with_name('cli.py'))
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        for data in ([{'rrid':1,'totalDis':2150,'totalTime':760,'status':1,'complete':True}],
                     {'recordList':[]},['invalid']):
            client=Mock();client.token='DUMMY';client.uid=1
            client.get_history_records.return_value={'error':10000,'data':data}
            with patch.object(cli,'SportClient792',return_value=client), \
                 patch.object(sys,'argv',['cli.py','-t','DUMMY','--uid','1','--history']), \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                cli.main()
            text=output.getvalue()
            if data==['invalid']:self.assertIn('响应解析失败',text)
            else:self.assertIn('历史记录: 条数=',text)
            client.execute_run.assert_not_called()

if __name__=='__main__':unittest.main()
