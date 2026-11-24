"""Offline regression tests for measured distance, timestamps and bucket totals."""
import base64
import copy
import gzip
import json
import math
import os
import socket
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from policy.models import GeoFenceArea, GeoPoint
from running.builder import RunningTrackBuilder
from running.client import RunningClient
from running.consistency import (
    FOLDBACK_MARGIN_M, _extend_for_foldback, distance, sample_route,
    time_buckets)
from running.models import GpsPoint, RouteDecision
from running.outbox import Outbox, STATUS_FAILED, STATUS_SAVED, STATUS_VERIFIED
from running.schema import build_loc_json, encode_run_data


def official_points():
    """Five points with both BD-09 lat/lng and GCJ-02 glat/glon."""
    return [{'lat': 30 + i * .0003, 'lon': 120 + i * .0002,
             'glat': 30 + i * .0003 - .006, 'glon': 120 + i * .0002 - .006,
             'pointName': f'Campus {i}', 'isFixed': i % 2}
            for i in range(5)]


def official_fence():
    return GeoFenceArea.from_api_data({
        'id': 3, 'name': 'test',
        'points': [{'lat': 30, 'lon': 120, 'glat': 29.994, 'glon': 119.994}],
        'passPoints': official_points(),
    })


def measured_length(points):
    # Independent spherical-law-of-cosines measurement, not the production helper.
    result = 0.0
    for first, last in zip(points, points[1:]):
        la1, la2 = math.radians(first.lat), math.radians(last.lat)
        delta = math.radians(last.lon-first.lon)
        cosine = math.sin(la1)*math.sin(la2) + math.cos(la1)*math.cos(la2)*math.cos(delta)
        result += 6371000*math.acos(max(-1.0, min(1.0, cosine)))
    return result


class RunConsistencyTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def build(self, seconds=760, meters=2150, steps=2900, mode=1, checkpoints=None,
              pace_bounds=None):
        builder = RunningTrackBuilder(1, 2, mode, uuid_str='offline-consistency-test')
        points, fixed = builder.generate_track_around_points(
            30, 120, checkpoints, meters, seconds, 1700000000000)
        body, obs, key = builder.build_submission_payload(
            meters, seconds, steps, 1700000000000, 1700000000000+seconds*1000,
            points, fixed, pace_bounds=pace_bounds)
        return builder, points, fixed, body, obs

    def test_reported_2150m_760s_2900step_case(self):
        _, points, fixed, body, obs = self.build()
        self.assertAlmostEqual(measured_length(points), 2150, delta=.1)
        self.assertEqual(len(fixed), 5)
        self.assertEqual(sum(row['stepsNum'] for row in body['stepsPerTenSec']), 2900)
        self.assertAlmostEqual(sum(row['distance'] for row in body['speedPerTenSec']), 2150)
        self.assertEqual(points[-1].time-points[0].time, 760000)
        self.assertTrue(all(a.time < b.time for a,b in zip(points,points[1:])))
        # OBS and settlement must carry the same data, including bucket remainders.
        for field, obs_field in [('allLocJson','run_data'), ('fivePointJson','fixed_point_json')]:
            self.assertEqual(body[field], gzip.decompress(base64.b64decode(obs[obs_field])).decode())
        for field, obs_field in [('stepsPerTenSec','step_freq_json'), ('speedPerTenSec','speed_json')]:
            decoded=json.loads(gzip.decompress(base64.b64decode(obs[obs_field])))
            self.assertEqual(body[field], decoded)
        # save_record and OBS carry the official LocJsonEntity, not a bare point array.
        outer = json.loads(body['allLocJson'])
        self.assertEqual(set(outer), {'useZip', 'allLocJson'})
        self.assertIs(outer['useZip'], False)
        inner = json.loads(outer['allLocJson'])
        self.assertEqual(len(inner), len(points))
        self.assertIn('lng', inner[0])
        self.assertIn('gLat', inner[0])
        self.assertIn('gLng', inner[0])
        self.assertIn('gainTime', inner[0])
        self.assertNotIn('lon', inner[0])

    def test_track_uses_whole_seconds_and_mild_pace(self):
        # MyLocation.totalTime 只有秒级精度：采样点必须整秒，否则相邻点会出现
        # 0/1 秒的时间差，App 配速图会画出 0'00" 和 27' 的假尖刺。
        for meters, seconds in ((3050, 1321), (3000, 1060), (2150, 760)):
            with self.subTest(meters=meters, seconds=seconds):
                builder = RunningTrackBuilder(1, 2, 1,
                                              uuid_str='offline-pace-test')
                points, _ = builder.generate_track_around_points(
                    30, 120, None, meters, seconds, 1700000000000)
                deltas = [b.time - a.time for a, b in zip(points, points[1:])]
                self.assertGreaterEqual(min(deltas), 1000)
                self.assertTrue(all(delta % 1000 == 0 for delta in deltas))
                self.assertAlmostEqual(measured_length(points), meters, delta=.1)
                average = seconds / (meters / 1000.0)
                paces = []
                for first, last in zip(points, points[1:]):
                    step = distance((first.lat, first.lon), (last.lat, last.lon))
                    if step > 0:
                        paces.append((last.time - first.time) / 1000.0
                                     / (step / 1000.0))
                self.assertLess(max(paces) / average, 1.15)
                self.assertGreater(min(paces) / average, 0.85)

    def test_submission_speed_is_pace_in_minutes_per_km_scaled(self):
        # Official UploadFormatEntity.setSpeed() stores minutes/km * 1000.
        _, _, _, body, _ = self.build(seconds=760, meters=2150, steps=2900)
        self.assertEqual(body['speed'], 5890)

    def test_partial_last_bucket_is_contiguous_and_lossless(self):
        for seconds in (1, 7, 10, 11, 31, 761):
            with self.subTest(seconds=seconds):
                rows=list(time_buckets(seconds, 2903, 1000, 1000+seconds*1000, 2153))
                self.assertEqual(rows[0][0],1000)
                self.assertEqual(rows[-1][1],1000+seconds*1000)
                self.assertTrue(all(a[1]==b[0] for a,b in zip(rows,rows[1:])))
                self.assertTrue(all(0 < end-begin <= 10000 for begin,end,_,_ in rows))
                self.assertEqual(sum(row[2] for row in rows),2903)
                self.assertAlmostEqual(sum(row[3] for row in rows),2153)

    def test_real_checkpoint_coordinates_preserved_and_visited(self):
        targets=[{'lat':30.0+i*.0002,'lng':120.0+i*.0003,'name':str(i)} for i in range(5)]
        _,points,fixed,_,_=self.build(checkpoints=targets)
        self.assertEqual([(p.lat,p.lon) for p in fixed],[(p['lat'],p['lng']) for p in targets])
        for point in fixed:
            self.assertTrue(any(p.lat==point.lat and p.lon==point.lon for p in points))
        self.assertAlmostEqual(measured_length(points),2150,delta=.1)

    def test_zero_checkpoint_coordinate_is_not_replaced_by_center(self):
        targets=[{'lat':0.0,'lng':120.0}, {'lat':.0005,'lng':120.0005}]
        _,points,fixed,_,_=self.build(checkpoints=targets)
        self.assertEqual(fixed[0].lat,0.0)
        self.assertEqual(points[0].lat,0.0)
        self.assertAlmostEqual(measured_length(points),2150,delta=.1)

    def test_route_too_short_for_supplied_points_is_rejected(self):
        targets=[{'lat':30.0,'lng':120.0},{'lat':31.0,'lng':121.0}]
        with self.assertRaisesRegex(ValueError,'too short'):
            self.build(checkpoints=targets)

    def test_partial_duration_and_shorter_template_repeated_laps(self):
        # 这个用例只验证 10 秒桶的连续性，配速故意不在学校规则内；
        # 放宽 pace_bounds，避免每公里分段约束提前报错。
        _,points,_,body,_=self.build(seconds=761,meters=5037,steps=6103,
                                     pace_bounds=(1.0,20.0))
        self.assertAlmostEqual(measured_length(points),5037,delta=.1)
        self.assertEqual(sum(x['stepsNum'] for x in body['stepsPerTenSec']),6103)
        self.assertEqual(body['stepsPerTenSec'][-1]['endTime']-body['stepsPerTenSec'][-1]['beginTime'],1000)

    def test_external_route_polyline_is_sampled_and_keeps_checkpoints(self):
        checkpoints = [{'lat': 30.0, 'lng': 120.0, 'name': 'p0'},
                       {'lat': 30.001, 'lng': 120.0, 'name': 'p1'},
                       {'lat': 30.001, 'lng': 120.001, 'name': 'p2'},
                       {'lat': 30.0, 'lng': 120.001, 'name': 'p3'},
                       {'lat': 30.0005, 'lng': 120.001, 'name': 'p4'}]
        # Put the fifth checkpoint on an edge so the route loop contains all five.
        route = [(30.0, 120.0), (30.001, 120.0), (30.001, 120.001),
                 (30.0005, 120.001), (30.0, 120.001)]
        builder = RunningTrackBuilder(1, 2, 1, uuid_str='route-polyline-test')
        points, fixed = builder.generate_track_around_points(
            30.0, 120.0, checkpoints, 1000, 400, 1700000000000,
            route_polyline=route)
        self.assertAlmostEqual(measured_length(points), 1000, delta=.1)
        self.assertEqual(len(fixed), 5)
        for point in fixed:
            self.assertTrue(any(p.lat == point.lat and p.lon == point.lon
                                for p in points))

    def test_exact_length_open_route_folds_back_instead_of_jumping(self):
        # 手动路线已按原路折返展开到目标长度。整秒采样切掉微小折角后，
        # 补偿段必须沿原路反向，而不是从终点直线跳回起点。
        route = [(30.0000, 120.0000), (30.0005, 120.0000),
                 (30.0005, 120.0004), (30.0005, 120.00042),
                 (30.0005, 120.00041), (30.0005, 120.0008)]
        target = sum(distance(a, b) for a, b in zip(route, route[1:]))
        samples = sample_route(route, target, 60, 1700000000000,
                               require_all=True, seed=0)
        points = [(lat, lon) for lat, lon, _ in samples]
        self.assertAlmostEqual(
            sum(distance(a, b) for a, b in zip(points, points[1:])),
            target, delta=.02)
        times = [ms for _, _, ms in samples]
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
        self.assertGreaterEqual(
            min(b - a for a, b in zip(times, times[1:])), 1000)
        # 所有采样点仍在原折线上（允许补偿多走几米）。
        for point in points:
            nearest = min(
                self._point_segment_distance(point, a, b)
                for a, b in zip(route, route[1:]))
            self.assertLessEqual(nearest, 0.01, point)

    @staticmethod
    def _point_segment_distance(point, first, last):
        cos_lat = math.cos(math.radians(point[0]))
        px, py = point[1] * cos_lat, point[0]
        ax, ay = first[1] * cos_lat, first[0]
        bx, by = last[1] * cos_lat, last[0]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-18:
            value = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
            value = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        return value * math.radians(1.0) * 6371000.0

    def test_foldback_extension_only_for_open_exact_length_routes(self):
        route = [(30.0, 120.0), (30.0005, 120.0), (30.0005, 120.0005)]
        length = sum(distance(a, b) for a, b in zip(route, route[1:]))
        self.assertIs(_extend_for_foldback(route, length * 2), route)
        closed = route + [route[0]]
        closed_length = sum(distance(a, b) for a, b in zip(closed, closed[1:]))
        self.assertIs(_extend_for_foldback(closed, closed_length), closed)
        extended = _extend_for_foldback(route, length)
        self.assertEqual(len(extended), len(route) + 1)
        # 延伸点沿原折线继续；可用长度受该线段剩余长度限制。
        extension = distance(extended[-1], route[-1])
        self.assertGreater(extension, 10.0)
        self.assertLessEqual(extension, FOLDBACK_MARGIN_M + 1.5)
        for point in extended:
            self.assertTrue(any(
                self._point_segment_distance(point, a, b) <= 0.01
                for a, b in zip(route, route[1:])))

    def test_short_free_run_does_not_require_full_template_loop(self):
        _,points,fixed,_,_=self.build(meters=100,seconds=45,steps=120,mode=6)
        self.assertEqual(fixed,[])
        self.assertAlmostEqual(measured_length(points),100,delta=.1)

    def test_invalid_input_rejected(self):
        for seconds,steps,meters in [(0,1,100),(-1,1,100),(760,-1,2150),(760,1,float('nan'))]:
            with self.subTest(seconds=seconds,steps=steps,meters=meters),self.assertRaises(ValueError):
                self.build(seconds,meters,steps)

    def test_inconsistent_track_cannot_be_packaged(self):
        builder,points,fixed,_,_=self.build()
        for mutate in ('distance','timestamp','checkpoint'):
            bad=copy.deepcopy(points)
            if mutate=='distance': bad[1].lat += .01
            elif mutate=='timestamp': bad[-1].time-=1
            else: fixed=[types.SimpleNamespace(lat=31,lon=121)]
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):
                builder.build_submission_payload(2150,760,2900,1700000000000,
                                                 1700000760000,bad,fixed)

    def test_validation_failure_prevents_obs_and_settlement(self):
        client=RunningClient(uid=1,token='DUMMY',unid=2,device_config={'DeviceId':'1'*32})
        client.profile.get_server_time=Mock(return_value=types.SimpleNamespace(
            server_timestamp_ms=1700000000000,offset_seconds=0))
        client.telemetry.send_startup_pv=Mock()
        client.telemetry.send_page_exposure=Mock()
        client.policy.get_run_policy=Mock(return_value=types.SimpleNamespace(
            min_distance=2000, day_goal=3000, sequence_strength=0,
            raw={'runRuleModel':{'minRunTime':300, 'dayGoal':3000,
                                 'sequenceStrength':0},'policy':1}))
        client.policy.get_geo_fence=Mock(return_value=[official_fence()])
        client.request_obs_upload_url=Mock()
        client.submit_record=Mock()
        with patch('running.client.RunningTrackBuilder.generate_track_around_points',side_effect=ValueError('mismatch')):
            result=client.execute_full_run(path_source='synthetic')
        self.assertFalse(result.success)
        self.assertIn('mismatch',result.message)
        client.request_obs_upload_url.assert_not_called()
        client.submit_record.assert_not_called()

    def test_route_provider_osm_decision_reuses_live_points(self):
        client = self.client_for_upload()
        client.policy.get_checkpoints_for_run = Mock(
            return_value=[GeoPoint.from_api_data(p) for p in official_points()])

        class FakePlanner:
            def __init__(self):
                self.plan_calls = []

            def plan(self, checkpoints, target_distance_m, policy=1):
                self.plan_calls.append((list(checkpoints), target_distance_m))
                return types.SimpleNamespace(
                    polyline=[[float(lat), float(lon)] for lat, lon in checkpoints],
                    loop_length_m=100.0, laps=1.0, diagnostics={})

            def validate_track(self, track, allow_building_ids=()):
                pass

        planner = FakePlanner()
        client.request_obs_upload_url = Mock(return_value={
            'error': 10000,
            'data': {'signedUrl': 'https://example.test/put',
                     'actualSignedRequestHeaders': {}}})
        client.upload_to_obs = Mock(return_value={'error': 10000})
        client.submit_record = Mock(return_value={'error': -10000, 'message': 'mock failure'})
        with patch('running.client.TrackPlanner.from_campus', return_value=planner):
            result = client.execute_full_run(
                2150, 760, 1, None, path_source='auto',
                route_provider=lambda context: RouteDecision.osm())
        self.assertFalse(result.success)
        # 一次取点后由 provider 决策改走 OSM，不再重新请求五点接口。
        self.assertEqual(client.policy.get_checkpoints_for_run.call_count, 1)
        self.assertEqual(len(planner.plan_calls), 1)
        self.assertTrue(client.submit_record.called)

    def client_for_upload(self):
        client = RunningClient(uid=1, token='DUMMY', unid=2,
                               device_config={'DeviceId': '1'*32})
        client.profile.get_server_time = Mock(return_value=types.SimpleNamespace(
            server_timestamp_ms=1700000000000, offset_seconds=0))
        client.telemetry.send_startup_pv = Mock()
        client.telemetry.send_page_exposure = Mock()
        client.policy.get_run_policy = Mock(return_value=types.SimpleNamespace(
            min_distance=2000, day_goal=3000, sequence_strength=0,
            raw={'runRuleModel': {'minRunTime': 300, 'dayGoal': 3000,
                                  'sequenceStrength': 0}, 'policy': 1}))
        client.policy.get_geo_fence = Mock(return_value=[official_fence()])
        client.get_rundetail_config = Mock(return_value={})
        return client

    def test_dry_run_exports_track_without_upload(self):
        client = self.client_for_upload()
        client.request_obs_upload_url = Mock()
        client.submit_record = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            export = os.path.join(tmp, 'track.geojson')
            result = client.execute_full_run(dry_run=True, export_track=export,
                                             path_source='synthetic')
            self.assertTrue(result.success)
            self.assertTrue(result.dry_run)
            self.assertGreater(result.track_points, 0)
            self.assertTrue(os.path.isfile(export))
            client.request_obs_upload_url.assert_not_called()
            client.submit_record.assert_not_called()
            with open(export, encoding='utf-8') as handle:
                data = json.load(handle)
            self.assertEqual(data['type'], 'FeatureCollection')
            coords = data['features'][0]['geometry']['coordinates']
            self.assertEqual(len(coords), result.track_points)
            self.assertEqual(
                data['features'][0]['properties']['coordinate_system'], 'GCJ-02')

    def test_retry_reuses_existing_rrid_by_start_time(self):
        uuid_str = '77777777-8888-9999-aaaa-bbbbbbbbbbbb'
        start = 1700000000000
        client = RunningClient(uid=1, token='DUMMY', unid=2,
                               device_config={'DeviceId': '1'*32})
        client.request_obs_upload_url = Mock(return_value={
            'error': 10000,
            'data': {'signedUrl': 'https://example.test/put',
                     'actualSignedRequestHeaders': {}}})
        client.upload_to_obs = Mock(return_value={'error': 10000})
        client.submit_record = Mock()
        client.history.get_run_records = Mock(return_value={
            'error': 10000,
            'data': [{'startTime': start, 'rrid': 999, 'totalDis': 3000}]})
        client.history.get_one_record = Mock(
            return_value=types.SimpleNamespace(is_valid=True))
        with tempfile.TemporaryDirectory() as tmp, patch('running.client.time.sleep'):
            outbox = Outbox(tmp)
            outbox.write_pending(uuid_str,
                                 {'startTime': start, 'sportType': 1,
                                  'totalDis': 3000, 'totalTime': 1060},
                                 {'run_data': 'payload'},
                                 'run_data/2026010112/retry.json')
            outbox.mark(uuid_str, STATUS_FAILED, error='结算响应丢失')
            result = client.submit_from_outbox(uuid_str, outbox_dir=tmp,
                                               verify_upload=False)
            self.assertTrue(result.success)
            self.assertEqual(result.rrid, 999)
            client.submit_record.assert_not_called()
            self.assertEqual(outbox.load(uuid_str)['status'], STATUS_SAVED)

    def test_verify_uploaded_track_reads_run_data_inside_obs_payload(self):
        start = 1700000000000
        pts = [GpsPoint(30.0, 120.0, start, 2.5, 3.0),
               GpsPoint(30.0002, 120.0003, start + 3000, 2.5, 3.0),
               GpsPoint(30.0004, 120.0001, start + 6000, 2.5, 3.0)]
        loc_json = build_loc_json(pts, start)
        obs_object = json.dumps({
            "uuid": "offline-verify",
            "run_data": encode_run_data(loc_json),
        }).encode("utf-8")
        client = RunningClient(uid=1, token='DUMMY', unid=2,
                               device_config={'DeviceId': '1'*32})
        client.request_obs_download_url = Mock(return_value={
            'error': 10000,
            'data': {'signedUrl': 'https://example.test/get',
                     'actualSignedRequestHeaders': {}}})
        client.download_from_obs = Mock(return_value={'error': 10000,
                                                      'data': obs_object})
        client.history.get_one_record = Mock(
            return_value=types.SimpleNamespace(is_valid=True))
        check = client.verify_uploaded_track(
            'run_data/2026010112/offline-verify.json',
            expected_loc_json=loc_json, rrid=123)
        self.assertTrue(check['ok'], check.get('error'))
        self.assertEqual(check['points'], 3)
        self.assertIsNotNone(check['record'])

    def test_successful_upload_writes_and_verifies_outbox(self):
        client = self.client_for_upload()
        client.request_obs_upload_url = Mock(return_value={
            'error': 10000,
            'data': {'signedUrl': 'https://example.test/put',
                     'actualSignedRequestHeaders': {}}})
        client.upload_to_obs = Mock(return_value={'error': 10000})
        client.submit_record = Mock(return_value={'error': 10000, 'data': {'rrid': 123}})
        client.history.get_one_record = Mock(
            return_value=types.SimpleNamespace(is_valid=True))
        client.history.get_run_records = Mock(
            return_value={'error': 10000, 'data': []})
        client.verify_uploaded_track = Mock(return_value={'ok': True, 'points': 10})
        with tempfile.TemporaryDirectory() as tmp, patch('running.client.time.sleep'):
            result = client.execute_full_run(outbox_dir=tmp,
                                             path_source='synthetic')
            self.assertTrue(result.success)
            self.assertEqual(result.rrid, 123)
            self.assertTrue(result.verified)
            self.assertGreater(result.track_points, 0)
            entry = Outbox(tmp).find_by_rrid(123)
            self.assertIsNotNone(entry)
            self.assertEqual(entry['status'], STATUS_VERIFIED)
            outer = json.loads(entry['save_record']['allLocJson'])
            self.assertIs(outer['useZip'], False)
            client.verify_uploaded_track.assert_called_once()


if __name__=='__main__':
    unittest.main()
