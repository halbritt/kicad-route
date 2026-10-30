"""Exact duplicate routes on a poured net are still generated copper."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'))
from kicad_parser import BoardInfo, PCBData, Segment
import pcb_modification as modification


def segment(**changes):
    values = dict(start_x=118.725, start_y=114.325, end_x=118.725,
                  end_y=114.950, width=.25, layer='B.Cu', net_id=5)
    return Segment(**(values | changes))


def board(segments):
    pcb = PCBData(BoardInfo({}, ['F.Cu','B.Cu']), {}, {}, [], segments, {})
    pcb.zones = [SimpleNamespace(net_id=5)]
    return pcb


class GeneratedDuplicateTests(unittest.TestCase):
    def test_cleanup_deduplicates_pieces_created_by_its_final_t_split(self):
        from cleanup_pipeline import run_post_route_cleanup
        from kicad_parser import Net, Pad
        from routing_config import GridRouteConfig
        whole=segment(start_x=0,start_y=0,end_x=2,end_y=0)
        part=segment(start_x=0,start_y=0,end_x=1,end_y=0)
        pcb=board([whole,part])
        pcb.board_info.board_bounds=(-1,-1,3,1)
        pads=[Pad(str(i),'1',x,0,0,0,.2,.2,'circle',['B.Cu'],5,'GND',pad_type='smd')
              for i,x in enumerate((0,2))]
        pcb.pads_by_net[5]=pads;pcb.nets[5]=Net(5,'GND',pads)
        results=[{'new_segments':[whole,part]}]
        outcome=run_post_route_cleanup(results,pcb,{5},GridRouteConfig(clearance=.2),
            protect_net_ids={5},snap=False,phantom=False,graze=False,octolinear=False,
            via_nudge=False,cycles=False,neck=False,microshift_max_shift=0,
            smooth=False,merge_collinear=False,strict_collapse=False,keep_input_copper=True)
        self.assertEqual(outcome.counts['generated_duplicates_removed'],1)
        spans=[tuple(sorted(((s.start_x,s.start_y),(s.end_x,s.end_y)))) for s in pcb.segments]
        self.assertEqual(sorted(spans),[((0,0),(1,0)),((1,0),(2,0))])
        self.assertEqual(results[0]['new_segments'],pcb.segments)

    def test_repeated_object_occurrences_are_emitted_only_once(self):
        for board_copies in (1,2):
            with self.subTest(board_copies=board_copies):
                item=segment();pcb=board([item]*board_copies)
                results=[{'new_segments':[item]},{'new_segments':[item]}]
                self.assertEqual(modification.remove_generated_duplicates(results,pcb,{5}),1)
                self.assertEqual(pcb.segments,[item])
                self.assertEqual(results,[{'new_segments':[item]},{'new_segments':[]}])

    def test_generated_exact_duplicates_on_a_poured_net_keep_one_object(self):
        first, second = segment(), segment(start_y=114.950,end_y=114.325)
        pcb=board([first,second]); results=[{'new_segments':[first]}, {'new_segments':[second]}]
        self.assertEqual(modification.remove_generated_duplicates(results,pcb,{5}),1)
        self.assertEqual(pcb.segments,[first])
        self.assertEqual(results,[{'new_segments':[first]},{'new_segments':[]}])
        self.assertEqual(modification.remove_generated_duplicates(results,pcb,{5}),0)

    def test_protected_input_wins_even_when_encountered_last(self):
        for locked in (False,True):
            first, second, source = segment(), segment(), segment(locked=locked)
            pcb=board([first,second,source]); results=[{'new_segments':[first,second]}]
            before=copy.deepcopy(source)
            self.assertEqual(modification.remove_generated_duplicates(results,pcb,{5}),2)
            self.assertIs(pcb.segments[0],source)
            self.assertEqual(source,before)
            self.assertEqual(results[0]['new_segments'],[])

    def test_existing_duplicates_and_locked_generated_objects_are_immutable(self):
        first, second, third=segment(),segment(),segment(locked=True)
        pcb=board([first,second,third]); results=[{'new_segments':[third]}]
        original=pcb.segments
        self.assertEqual(modification.remove_generated_duplicates(results,pcb,{5}),0)
        self.assertIs(pcb.segments,original)

    def test_different_geometry_scope_and_graphics_never_match(self):
        for values,scope in (({'end_y':114.950001},{5}),({'width':.251},{5}),
                             ({'layer':'F.Cu'},{5}),({'net_id':6},{5,6}),
                             ({'graphic':True},{5}),({}, {6})):
            with self.subTest(values=values,scope=scope):
                first,second=segment(),segment(**values)
                pcb=board([first,second]); results=[{'new_segments':[first,second]}]
                before=copy.deepcopy(pcb.segments)
                self.assertEqual(modification.remove_generated_duplicates(results,pcb,scope),0)
                self.assertEqual(pcb.segments,before)

    def test_population_limit_leaves_board_and_write_lists_unchanged(self):
        first,second=segment(),segment()
        pcb=board([first,second]); results=[{'new_segments':[first,second]}]
        original,emitted=pcb.segments,results[0]['new_segments'];stats={}
        self.assertEqual(modification.remove_generated_duplicates(results,pcb,{5},max_segments=1,stats=stats),0)
        self.assertIs(pcb.segments,original)
        self.assertIs(results[0]['new_segments'],emitted)
        self.assertTrue(stats['limit_reached'])


if __name__=='__main__':unittest.main()
