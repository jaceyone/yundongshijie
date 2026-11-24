"""Offline coverage for the Android five-point request and runtime integration."""
import base64
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest.mock import Mock, patch

from policy.client import PolicyClient
from policy.models import GeoFenceArea, GeoPoint
from running.builder import RunningTrackBuilder
from running.client import RunningClient
from running.models import RouteDecision


def official_points():
    return [{'lat':30+i*.0003, 'lon':120+i*.0002,
             'glat':30+i*.0003-.006, 'glon':120+i*.0002-.006,
             'pointName':f'Campus {i}', 'isFixed':i%2} for i in range(5)]


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        guard=patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def client(self, response=None):
        client=PolicyClient(uid=1,token='DUMMY',unid=2,device_config={'DeviceId':'1'*32})
        client.execute_request=Mock(return_value=response or {'error':10000,'data':{
            'pointsResModels':official_points(),'sportStartTime':1700000000000}})
        return client

    def fence(self):
        return GeoFenceArea.from_api_data({'id':3,'name':'test','points':[
            {'lat':30,'lon':120,'glat':29.994,'glon':119.994}]})

    def test_full_request_body_and_fixed_path_key(self):
        client=self.client()
        points=client.get_run_checkpoints(2000,29.994,119.994,uuid_str='session-id')
        self.assertEqual(len(points),5)
        args=client.execute_request.call_args
        url='https://run.gxapp.iydsj.com/api/v560/get/1/distance/2000'
        self.assertEqual(args.args,('POST',url))
        body=args.kwargs['body_plain']
        self.assertEqual(set(body),{'longitude','latitude','selectedUnid','uuid','runec','runAreaId','sign'})
        self.assertEqual(body['selectedUnid'],2)
        self.assertEqual(body['uuid'],'session-id')
        self.assertEqual(body['runAreaId'],-1)
        self.assertEqual(body['runec'],'')
        expected=hashlib.md5(b'http://run.gxapp.iydsj.com/api/v560/get/1/distance/2000' +
                             b'2slhe02lsfiwowlcixisla_sls-_slaor').hexdigest()
        self.assertEqual(body['sign'],expected)
        self.assertEqual(client.last_checkpoint_start_time,1700000000000)

    def test_payload_shapes_and_metadata_preserved(self):
        for data in ({'pointsResModels':official_points()},json.dumps({'pointsResModels':official_points()})):
            client=self.client({'error':10000,'data':data})
            points=client.get_run_checkpoints(2000,30,120)
            self.assertEqual(points[1].name,'Campus 1')
            self.assertEqual(points[1].raw['isFixed'],1)
            self.assertEqual(points[0].gcj_coordinates(),(29.994,119.994))
            self.assertIsNone(client.last_checkpoint_error)

    def test_error_empty_and_bad_coordinates_not_success(self):
        responses=[{'error':-10000,'message':'ERROR'},
                   {'error':10000,'data':{'pointsResModels':[]}},
                   {'error':10000,'data':{'passPoints':official_points()}},
                   {'error':10000,'data':{'pointsResModels':[{'lat':30}]}},
                   {'error':10000,'data':'{broken'}]
        for response in responses:
            with self.subTest(response=response):
                client=self.client(response)
                self.assertEqual(client.get_run_checkpoints(2000,30,120),[])
                self.assertTrue(client.last_checkpoint_error)
                self.assertIsNone(client.last_checkpoint_start_time)

    def test_missing_anchor_and_invalid_distance_prevent_network(self):
        for kwargs in ({},{'latitude':float('nan'),'longitude':120},
                       {'latitude':30,'longitude':181},{'latitude':30,'longitude':120,'distance_m':0}):
            client=self.client()
            self.assertEqual(client.get_run_checkpoints(**({'distance_m':2000,'latitude':None,'longitude':None}|kwargs)),[])
            client.execute_request.assert_not_called()

    def test_geofence_lon_and_lng_preserve_gcj_anchor(self):
        for key in ('lon','lng'):
            fence=GeoFenceArea.from_api_data({'id':3,'points':[
                {'lat':30,key:120,'glat':29.994,'glng':119.994}]})
            self.assertEqual(fence.polygon_points[0].lng,120)
            self.assertEqual(fence.polygon_points[0].gcj_coordinates(),(29.994,119.994))
            self.assertEqual(fence.check_points,[])
        for raw in ({'lat':30,'lon':120},{'lat':30,'lon':120,'glat':-1,'glon':-1}):
            with self.assertRaises(ValueError):GeoPoint.from_api_data(raw).gcj_coordinates()

    def test_policy_minimum_distance_and_selected_area(self):
        client=self.client()
        client.get_run_policy=Mock(return_value=types.SimpleNamespace(min_distance=2000,raw={
            'runAreaModels':[{'id':8,'valid':True,'select':True,'fenceIds':[3]}]}))
        self.assertEqual(len(client.get_checkpoints_for_run(self.fence(),'shared-uuid')),5)
        call=client.execute_request.call_args
        self.assertIn('/distance/2000',call.args[1])
        self.assertEqual(call.kwargs['body_plain']['runAreaId'],8)
        self.assertEqual(call.kwargs['body_plain']['latitude'],29.994)
        self.assertEqual(call.kwargs['body_plain']['uuid'],'shared-uuid')

    def test_anchor_gcj_overrides_fence_vertex_and_is_recorded(self):
        client=self.client()
        client.get_run_policy=Mock(return_value=types.SimpleNamespace(min_distance=2000,raw={}))
        self.assertEqual(len(client.get_checkpoints_for_run(
            self.fence(),'anchor-uuid',anchor_gcj=(29.9945,119.9945))),5)
        body=client.execute_request.call_args.kwargs['body_plain']
        self.assertAlmostEqual(body['latitude'],29.9945)
        self.assertAlmostEqual(body['longitude'],119.9945)
        self.assertEqual(body['uuid'],'anchor-uuid')
        self.assertEqual(client.last_checkpoint_anchor,{
            'lat':29.9945,'lon':119.9945,'source':'anchor'})

    def test_invalid_or_outside_anchor_prevents_request(self):
        # 围栏是 29.994/119.994 附近的小多边形；(30.5,120.5) 在围栏外。
        fence=GeoFenceArea.from_api_data({'id':3,'name':'test','points':[
            {'lat':30,'lon':120,'glat':29.994,'glon':119.994},
            {'lat':30,'lon':120.01,'glat':29.994,'glon':120.004},
            {'lat':30.01,'lon':120.01,'glat':30.004,'glon':120.004},
            {'lat':30.01,'lon':120,'glat':30.004,'glon':119.994}]})
        for anchor in ((30.5,120.5),(float('nan'),119.994),(30,181),'bad',(1,)):
            with self.subTest(anchor=anchor):
                client=self.client()
                client.get_run_policy=Mock(return_value=types.SimpleNamespace(
                    min_distance=2000,raw={}))
                self.assertEqual(client.get_checkpoints_for_run(
                    fence,anchor_gcj=anchor),[])
                client.execute_request.assert_not_called()
                self.assertTrue(client.last_checkpoint_error)

    def test_ambiguous_area_and_missing_rule_prevent_point_request(self):
        for rule in (None,types.SimpleNamespace(min_distance=2000,raw={'runAreaModels':[
                {'id':1,'valid':True,'fenceIds':[3]},{'id':2,'valid':True,'fenceIds':[3]}]})):
            client=self.client()
            client.get_run_policy=Mock(return_value=rule)
            self.assertEqual(client.get_checkpoints_for_run(self.fence()),[])
            client.execute_request.assert_not_called()
            self.assertTrue(client.last_checkpoint_error)

    def test_official_metadata_survives_builder_and_obs(self):
        checkpoints=[GeoPoint.from_api_data(point).as_checkpoint() for point in official_points()]
        builder=RunningTrackBuilder(1,2,1)
        track,fixed=builder.generate_track_around_points(30,120,checkpoints,2150,760,0)
        body,obs,_=builder.build_submission_payload(2150,760,2900,0,760000,track,fixed)
        envelope=json.loads(body['fivePointJson'])
        serialized=json.loads(envelope['fivePointJson'])
        for actual,source in zip(serialized,official_points()):
            for key in ('lat','lon','glat','glon','pointName','isFixed'):
                self.assertEqual(actual[key],source[key])
        self.assertEqual(envelope,json.loads(gzip.decompress(base64.b64decode(obs['fixed_point_json']))))

    def test_counted_run_fetches_official_points_and_reuses_uuid(self):
        client=RunningClient(uid=1,token='DUMMY',unid=2,device_config={'DeviceId':'1'*32})
        client.profile.get_server_time=Mock(return_value=types.SimpleNamespace(
            server_timestamp_ms=1700000760000,offset_seconds=0))
        client.telemetry.send_startup_pv=Mock()
        client.telemetry.send_page_exposure=Mock()
        client.policy.get_run_policy=Mock(return_value=types.SimpleNamespace(
            min_distance=2000, day_goal=3000, sequence_strength=0,
            raw={'runRuleModel':{'minRunTime':300, 'dayGoal':3000,
                                'sequenceStrength':0},'policy':1}))
        client.policy.get_geo_fence=Mock(return_value=[self.fence()])
        client.policy.get_checkpoints_for_run=Mock(return_value=[GeoPoint.from_api_data(p) for p in official_points()])
        client.request_obs_upload_url=Mock(return_value={'error':10000,'data':{'signedUrl':'https://obs.invalid/a'}})
        client.upload_to_obs=Mock(return_value={'error':10000})
        client.submit_record=Mock(return_value={'error':-10000,'message':'mock failure'})
        messages=[]
        result=client.execute_full_run(2150,760,1,messages.append,
                                       path_source='synthetic')
        self.assertFalse(result.success)
        body=client.submit_record.call_args.args[0]
        request_uuid=client.policy.get_checkpoints_for_run.call_args.kwargs['uuid_str']
        self.assertEqual(request_uuid,body['uuid'])
        self.assertTrue(any('来源=pointsResModels' in message for message in messages))
        serialized=json.loads(json.loads(body['fivePointJson'])['fivePointJson'])
        self.assertEqual(sorted(point['pointName'] for point in serialized),
                         [f'Campus {i}' for i in range(5)])
        self.assertEqual([point['position'] for point in serialized], list(range(5)))
        self.assertEqual(body['selRunTime'],300)
        self.assertTrue(body['complete'])
        self.assertEqual(body['unCompleteReason'],0)
        envelope=json.loads(body['fivePointJson'])
        self.assertFalse(envelope['useZip'])
        self.assertFalse(envelope['freedomShowFence'])
        self.assertEqual(envelope['runAreaId'],-1)
        self.assertEqual(json.loads(envelope['geoFencesJson']),[self.fence().raw])

    def test_missing_school_rule_stops_before_upload(self):
        client=RunningClient(uid=1,token='DUMMY',unid=2,device_config={'DeviceId':'1'*32})
        client.policy.get_run_policy=Mock(return_value=None)
        client.request_obs_upload_url=Mock()
        client.submit_record=Mock()
        result=client.execute_full_run(2150,760)
        self.assertFalse(result.success)
        client.request_obs_upload_url.assert_not_called()
        client.submit_record.assert_not_called()

    def test_target_distance_below_min_stops_before_points(self):
        client=RunningClient(uid=1,token='DUMMY',unid=2,device_config={'DeviceId':'1'*32})
        client.policy.get_run_policy=Mock(return_value=types.SimpleNamespace(
            min_distance=2000, day_goal=3000, sequence_strength=0,
            raw={'runRuleModel':{'minRunTime':300},'policy':1}))
        client.profile.get_server_time=Mock()
        client.policy.get_geo_fence=Mock()
        client.policy.get_checkpoints_for_run=Mock()
        client.request_obs_upload_url=Mock()
        client.submit_record=Mock()
        result=client.execute_full_run(1999,760,1,path_source='synthetic')
        self.assertFalse(result.success)
        self.assertIn('低于学校最低',result.message)
        client.profile.get_server_time.assert_not_called()
        client.policy.get_geo_fence.assert_not_called()
        client.policy.get_checkpoints_for_run.assert_not_called()
        client.submit_record.assert_not_called()


    def test_query_only_cli_never_submits(self):
        spec=importlib.util.spec_from_file_location('checkpoint_cli',Path(__file__).with_name('cli.py'))
        cli=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        client=Mock()
        client.get_geo_fences.return_value=[self.fence()]
        client.policy.get_checkpoints_for_run.return_value=[GeoPoint.from_api_data(p) for p in official_points()]
        with patch.object(sys,'argv',['cli.py','-t','DUMMY','--uid','1','--unid','2','--checkpoints']), \
                patch.object(cli,'SportClient792',return_value=client), \
                patch('sys.stdout',new_callable=io.StringIO) as output:
            cli.main()
        self.assertIn('点数=5',output.getvalue())
        client.run.assert_not_called()
        client.running.submit_record.assert_not_called()


class RouteProviderTests(unittest.TestCase):
    """手动路线 provider：取点之后回调、按返回顺序写 position、跳过官方排序。"""

    def setUp(self):
        guard=patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

        class FakeCampus:
            def building_at(self,lat,lon):
                return None

        class FakePlanner:
            def __init__(self):
                self.campus=FakeCampus()
                self.tracks=[]
                self.plan_calls=[]

            def plan(self,checkpoints,target_distance_m,policy=1):
                self.plan_calls.append((list(checkpoints),target_distance_m,policy))
                polyline=[[float(lat),float(lon)] for lat,lon in checkpoints]
                return types.SimpleNamespace(
                    polyline=polyline,loop_length_m=100.0,laps=1.0,
                    diagnostics={})

            def validate_track(self,track,allow_building_ids=()):
                self.tracks.append((list(track),set(allow_building_ids)))

        self.planner=FakePlanner()
        patcher=patch('running.client.TrackPlanner.from_campus',return_value=self.planner)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self):
        client=RunningClient(uid=1,token='DUMMY',unid=2,device_config={'DeviceId':'1'*32})
        client.profile.get_server_time=Mock(return_value=types.SimpleNamespace(
            server_timestamp_ms=1700000760000,offset_seconds=0))
        client.telemetry.send_startup_pv=Mock()
        client.telemetry.send_page_exposure=Mock()
        client.policy.get_run_policy=Mock(return_value=types.SimpleNamespace(
            min_distance=2000, day_goal=3000, sequence_strength=0,
            raw={'runRuleModel':{'minRunTime':300, 'dayGoal':3000,
                                'sequenceStrength':0},'policy':1}))
        client.policy.get_geo_fence=Mock(return_value=[GeoFenceArea.from_api_data({
            'id':3,'name':'test','points':[{'lat':30,'lon':120,'glat':29.994,'glon':119.994}]})])
        client.policy.get_checkpoints_for_run=Mock(
            return_value=[GeoPoint.from_api_data(p) for p in official_points()])
        client.request_obs_upload_url=Mock(return_value={'error':10000,'data':{
            'signedUrl':'https://obs.invalid/a'}})
        client.upload_to_obs=Mock(return_value={'error':10000})
        client.submit_record=Mock(return_value={'error':-10000,'message':'mock failure'})
        return client

    def test_provider_runs_after_live_points_and_rewrites_position(self):
        client=self.client()
        points=[GeoPoint.from_api_data(p).as_checkpoint() for p in official_points()]
        seen={}

        def provider(context):
            seen['context']=context
            seen['live_called']=client.policy.get_checkpoints_for_run.called
            return (
                [[cp['glat'],cp['glon']] for cp in reversed(points)],
                list(reversed(points)),
            )

        with patch('running.client.select_official_order') as ordering:
            result=client.execute_full_run(
                2150,760,1,None,path_source='synthetic',route_provider=provider)

        self.assertFalse(result.success)
        self.assertTrue(seen['live_called'])
        self.assertEqual(seen['context'].target_distance_m,2150)
        self.assertEqual(seen['context'].unid,2)
        self.assertEqual(len(seen['context'].raw_checkpoints),5)
        ordering.assert_not_called()
        body=client.submit_record.call_args.args[0]
        serialized=json.loads(json.loads(body['fivePointJson'])['fivePointJson'])
        self.assertEqual([point['position'] for point in serialized],list(range(5)))
        self.assertEqual([point['pointName'] for point in serialized],
                         [f'Campus {i}' for i in reversed(range(5))])
        self.assertTrue(self.planner.tracks)

    def test_target_distance_above_3000_is_allowed(self):
        client=self.client()
        points=[GeoPoint.from_api_data(p).as_checkpoint() for p in official_points()]
        route=[[cp['glat'],cp['glon']] for cp in points]
        # 加一个远端顶点，减少 4000 m 时的循环圈数。
        route.append([points[0]['glat']+0.01,points[0]['glon']+0.01])
        seen={}

        def provider(context):
            seen['target']=context.target_distance_m
            return route,points

        result=client.execute_full_run(
            4000,1060,1,None,path_source='synthetic',route_provider=provider)
        self.assertEqual(seen['target'],4000)
        self.assertTrue(client.submit_record.called)
        body=client.submit_record.call_args.args[0]
        self.assertEqual(body['totalDis'],4000)

    def test_provider_not_called_when_live_points_fail(self):
        client=self.client()
        client.policy.get_checkpoints_for_run=Mock(return_value=[])
        provider=Mock(return_value=([[29.994,119.994],[30,120]],[]))
        result=client.execute_full_run(
            2150,760,1,None,path_source='synthetic',route_provider=provider)
        self.assertFalse(result.success)
        self.assertIn('manual_route_checkpoint_mismatch',result.message)
        provider.assert_not_called()
        client.submit_record.assert_not_called()

    def test_provider_none_stops_before_upload(self):
        client=self.client()
        result=client.execute_full_run(
            2150,760,1,None,path_source='synthetic',route_provider=lambda context: None)
        self.assertFalse(result.success)
        self.assertIn('manual_route_missing',result.message)
        client.submit_record.assert_not_called()
        client.upload_to_obs.assert_not_called()

    def test_provider_rejects_long_and_mismatched_routes(self):
        points=[GeoPoint.from_api_data(p).as_checkpoint() for p in official_points()]
        long_route=[[29.994,119.994],[30.100,120.100]]

        client=self.client()
        result=client.execute_full_run(
            2150,760,1,None,path_source='synthetic',
            route_provider=lambda context:(long_route,points))
        self.assertFalse(result.success)
        self.assertIn('manual_route_too_short',result.message)

        client=self.client()
        bad_ordered=[dict(point) for point in points]
        bad_ordered[2]=dict(bad_ordered[2],glat=bad_ordered[2]['glat']+0.01,
                            glon=bad_ordered[2]['glon']+0.01)
        route=[[cp['glat'],cp['glon']] for cp in points]
        result=client.execute_full_run(
            2150,760,1,None,path_source='synthetic',
            route_provider=lambda context:(route,bad_ordered))
        self.assertFalse(result.success)
        self.assertIn('manual_route_checkpoint_mismatch',result.message)

    def test_execute_run_passes_anchor_and_records_it_in_context(self):
        client=self.client()
        seen={}

        def provider(context):
            seen['context']=context
            points=[GeoPoint.from_api_data(p).as_checkpoint() for p in official_points()]
            return RouteDecision.manual(
                [[cp['glat'],cp['glon']] for cp in points],points)

        messages=[]
        result=client.execute_full_run(
            2150,760,1,messages.append,path_source='synthetic',
            route_provider=provider,anchor_gcj=(29.9945,119.9945),
            anchor_id='a3',anchor_name='教学楼')
        self.assertFalse(result.success)  # 结算 mock 失败，但链路已到上传
        kwargs=client.policy.get_checkpoints_for_run.call_args.kwargs
        self.assertEqual(kwargs['anchor_gcj'],(29.9945,119.9945))
        self.assertEqual(seen['context'].anchor_gcj,(29.9945,119.9945))
        self.assertEqual(seen['context'].anchor_id,'a3')
        self.assertEqual(seen['context'].anchor_name,'教学楼')
        self.assertTrue(any('取点锚点' in message for message in messages))

    def test_invalid_anchor_stops_before_policy_request(self):
        client=self.client()
        result=client.execute_full_run(
            2150,760,1,None,path_source='synthetic',anchor_gcj=(200,120))
        self.assertFalse(result.success)
        self.assertIn('anchor_invalid',result.message)
        client.policy.get_run_policy.assert_not_called()

    def test_provider_osm_decision_reuses_live_points_and_skips_manual(self):
        client=self.client()
        live=[GeoPoint.from_api_data(p) for p in official_points()]
        calls={'live':0}

        def fetch(*args,**kwargs):
            calls['live']+=1
            return live
        client.policy.get_checkpoints_for_run=Mock(side_effect=fetch)
        provider=Mock(return_value=RouteDecision.osm())
        result=client.execute_full_run(
            2150,760,1,None,path_source='auto',route_provider=provider)
        self.assertFalse(result.success)
        provider.assert_called_once()
        self.assertEqual(calls['live'],1)
        self.assertTrue(self.planner.plan_calls)
        client.submit_record.assert_called_once()

    def test_anchor_point_failure_does_not_fallback_to_fence_points(self):
        client=self.client()
        # 围栏里仍带旧 passPoints；锚点取点失败时不能静默使用它们。
        client.policy.get_geo_fence=Mock(return_value=[GeoFenceArea.from_api_data({
            'id':3,'name':'test',
            'points':[{'lat':30,'lon':120,'glat':29.994,'glon':119.994}],
            'passPoints':official_points()})])
        client.policy.get_checkpoints_for_run=Mock(return_value=[])
        client.policy.last_checkpoint_error='五点接口 code=10603: 前5分钟获取点位已达到3次'
        result=client.execute_full_run(
            2150,760,1,None,path_source='auto',anchor_gcj=(29.994,119.994))
        self.assertFalse(result.success)
        self.assertIn('取点锚点请求失败',result.message)
        client.submit_record.assert_not_called()
        client.upload_to_obs.assert_not_called()

    def test_provider_cancel_stops_before_upload(self):
        client=self.client()
        result=client.execute_full_run(
            2150,760,1,None,path_source='synthetic',
            route_provider=lambda context:RouteDecision.cancel())
        self.assertFalse(result.success)
        self.assertIn('manual_route_cancelled',result.message)
        client.upload_to_obs.assert_not_called()
        client.submit_record.assert_not_called()


if __name__=='__main__':unittest.main()
