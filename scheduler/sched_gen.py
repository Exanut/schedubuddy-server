from scheduler import query

import pycosat
import numpy as np
from itertools import product
from random import choice, shuffle

EXHAUST_CARDINALITY_THRESHOLD = 500000
ASSUMED_COMMUTE_TIME = 40
IDEAL_CONSECUTIVE_LENGTH = 3.5*60
CONFLICTS = query.get_conflicts_set()

class ValidSchedule:
    def __init__(self, schedule, aliases, blocks, num_pages):
        self._schedule = schedule
        self._aliases = aliases
        self._blocks = blocks
        self._num_pages = num_pages
        self.gap_err = self.compute_gap_err()
        self.time_variance = self.compute_time_variance()
        self.time_wasted = self.compute_time_wasted()
        self.gap_err_rank = None
        self.time_var_rank = None
        self.time_wasted_rank = None
        self.overall_rank = None
        self.score = None

    def compute_gap_err(self):
        GVE = 0
        for day_blocks in self._blocks.values():
            for block in day_blocks:
                block_len = block[1] - block[0]
                if block_len <= IDEAL_CONSECUTIVE_LENGTH:
                    GVE += (IDEAL_CONSECUTIVE_LENGTH - block_len)**2
                else:
                    # greatly discourage very long marathons
                    GVE += (block_len - IDEAL_CONSECUTIVE_LENGTH)**3
        return GVE
    
    def compute_time_variance(self):
        start_times, end_times = [], []
        for day in self._blocks.keys():
            start_times.append(self._blocks[day][0][0])
            end_times.append(self._blocks[day][-1][1])
        return np.var(start_times)*1.5 + np.var(end_times)
    
    def compute_time_wasted(self):
        time_wasted = 0
        for day in self._blocks.keys():
            time_wasted += ASSUMED_COMMUTE_TIME * 2
            day_blocks = self._blocks[day]
            day_time_wasted = day_blocks[-1][1] - day_blocks[0][0]
            for block in day_blocks:
                day_time_wasted -= block[1] - block[0]
            time_wasted += day_time_wasted
        return time_wasted
    
    def get_gap_err(self):
        return self.gap_err
    
    def get_time_variance(self):
        return self.time_variance

    def set_overall_rank(self):
        combined_rank = self.time_wasted_rank +\
            self.gap_err_rank +\
            self.time_var_rank
        self.score = round(((combined_rank / (self._num_pages*3*1.5)) * 5), 2)

        adjusted_combined_rank =\
            self.time_wasted_rank*1 +\
            self.gap_err_rank*1 +\
            self.time_var_rank*1
        self.adjusted_score = round(((adjusted_combined_rank /
            (self._num_pages*3)) * 5), 2)
    
    def get_schedule(self):
        return self._schedule


# param course_list is a list of strings of form "SUBJ CATALOG" e.g. "CHEM 101".
# returns a tuple (components, aliases). components is list of components where
#   a component is all classes belonging to a particular component of a course.
#   e.g. create_components(["CHEM 101"]) will find components for LEC, SEM, LAB,
#   and CSA. aliases maps string class IDs to all classes of the same component
#   that share identical start times, end times, and days, used to reduce the
#   search space to only unique "looking" schedules.
def _create_components(course_list):
    assert len(course_list) > 0, "Courses input is empty"
    assert len(set(course_list)) == len(course_list),\
        "Course list has duplicates"
    components = []
    aliases = {}
    for course in course_list:
        course_dict = query.get_course_classes(course)
        for component in course_dict.keys():
            component_classes = course_dict[component]
            new_component = []
            component_aliases = {}
            classtime_to_first_class = {}
            for component_class in component_classes:
                class_comp_str = component_class[0] + ' ' + component_class[1] # e.g., LEC A1
                class_times = []
                for i in range(len(component_class[4])):
                    class_times.append((component_class[4][i][:3]))
                class_times = tuple(class_times)
                if class_times in classtime_to_first_class:
                    first_class = classtime_to_first_class[class_times]
                    component_aliases[first_class].append(class_comp_str)
                    aliases[first_class] = component_aliases[first_class]
                else:
                    first_class_key = course + ' ' + class_comp_str
                    classtime_to_first_class[class_times] = first_class_key
                    component_aliases[first_class_key] = []
                    new_component.append(component_class)
            components.append(new_component)
    return (components, aliases)

# 3 classes of clauses are constructed:
# 1. min_sol is conjunction of classes that must be in a solution,
#    i.e. a solution must have a class from each component.
# 2. single_sel is logical implication of the fact that if a solution
#    contains class P then all classes C in the same component of P cannot
#    be in the solution: P -> ~C1 /\ ~C2, /\ ... /\ ~Cn.
# 3. conflicts is a set of tuple lists of form [C1, C2] where C1 and C2
#    have a time conflict
def _build_cnf(components):
    min_sol = []
    single_sel = []
    flat_idx = 1
    for component in components:
        component_min_sol = list(range(flat_idx, len(component)+flat_idx))
        flat_idx += len(component)
        min_sol.append(component_min_sol)
        not_min_sol = [-1 * e for e in component_min_sol]
        component_single_sel = []
        for i_ss in range(len(not_min_sol)):
            for j_ss in range(i_ss+1, len(not_min_sol)):
                if i_ss != j_ss:
                    component_single_sel.append(\
                        [not_min_sol[i_ss], not_min_sol[j_ss]])
                    component_single_sel.append(\
                        [not_min_sol[j_ss], not_min_sol[i_ss]])
        single_sel += component_single_sel
    flat_components = [e for c in components for e in c]
    conflicts = []
    for i in range(len(flat_components)):
        for j in range(i+1, len(flat_components)):
            class_a, class_b = flat_components[i][-1], flat_components[j][-1]
            if (class_a, class_b) in CONFLICTS:
                conflicts.append([-1 * (i+1), -1 * (j+1)])
                conflicts.append([-1 * (j+1), -1 * (i+1)])
    cnf = min_sol + single_sel + conflicts
    return cnf

# Given a list of components, returns the size of the cross product. This is
# useful for knowing the workload prior to computing it, which can be grow
# more than exponentially fast.
def _cross_prod_cardinality(components):
    cardinality = 1
    for component in components:
        cardinality *= len(component)
    return cardinality

# Given a schedule that is represented by a list of classes retrived from a
# database, check if it is valid by looking up the existence of
# time-conflict-pairs for every pair of classes in the schedule.
def _valid_schedule(schedule):
    class_ids = class_ids = [c[-1] for c in schedule]
    for i in range(len(class_ids)):
        for j in range(i+1, len(class_ids)):
            if (class_ids[i], class_ids[j]) in CONFLICTS:
                return False
    return True

# Given a list of schedules, filter out every schedule with time conflicts. 
def _validate_schedules(schedules):
    valid_schedules = []
    for schedule in schedules:
        if _valid_schedule(schedule):
            valid_schedules.append(schedule)
    return valid_schedules

def _get_schedule_blocks(schedule):
    clean_sched = []
    for course_class in schedule:
        has_time_null = False
        for time_tuple in course_class[4]:
            start_t = time_tuple[0]
            if start_t == 2147483647:
                has_time_null = True
        if not has_time_null:
            clean_sched.append(course_class)
    schedule = clean_sched
    day_times_map = {}
    for course_class in schedule:
        for time_tuple in course_class[4]:
            start_t, end_t, days, _ = time_tuple
            for day in days:
                if not day in day_times_map:
                    day_times_map[day] = [(start_t, end_t)]
                else:
                    day_times_map[day].append((start_t, end_t))
    for times in day_times_map.values():
        if len(times) == 1:
            continue
        times.sort()
        i = 0
        while i <= len(times)-2:
            t_i, t_j = times[i], times[i+1]
            if t_j[0] - t_i[1] <= 15:
                times[i] = (t_i[0], t_j[1])
                del times[i+1]
                i -= 1
            i += 1
    return day_times_map

def _master_sort(schedules):
    sched_objs = []
    num_pages = len(schedules)
    for schedule in schedules:
        blocks = _get_schedule_blocks(schedule)
        sched_obj = ValidSchedule(schedule, [], blocks, num_pages)
        sched_objs.append(sched_obj)
    gap_err_sorted = sorted(sched_objs, key=lambda SO: SO.gap_err, reverse=True)
    time_var_sorted = sorted(sched_objs, key=lambda SO: SO.time_variance, reverse=True)
    time_waste_sorted = sorted(sched_objs, key=lambda SO: SO.time_wasted, reverse=True)
    for i, sched_obj in enumerate(gap_err_sorted):
        sched_obj.gap_err_rank = i+1
    for i, sched_obj in enumerate(time_var_sorted):
        sched_obj.time_var_rank = i+1
    for i, sched_obj in enumerate(time_waste_sorted):
        sched_obj.time_wasted_rank = i+1
    for sched_obj in sched_objs:
        sched_obj.set_overall_rank()
    overall_sorted = sorted(sched_objs, key=lambda SO: SO.adjusted_score, reverse=True)
    overall_sorted = overall_sorted[:min(100, num_pages)]
    #shuffle(overall_sorted)
    return overall_sorted

def _key_obj(course_class):
    obj = {}
    obj["class"] = course_class[6]
    obj["component"] = course_class[0]
    obj["section"] = course_class[1]
    obj["campus"] = course_class[2]
    obj["instructor"] = course_class[3]
    times_list = []
    for time in course_class[4]:
        time_obj = {}
        time_obj["startTime"] = time[0]
        time_obj["endTime"] = time[1]
        time_obj["day"] = time[2]
        time_obj["location"] = time[3]
        times_list.append(time_obj)
    obj["times"] = times_list
    return obj

def _json_sched(sched):
    prev_course = None
    course_sched = {}
    for course_class in sched:
        course = course_class[5]
        if prev_course != course:
            course_sched[course] = []
            prev_course = course
        course_sched[course].append(_key_obj(course_class))
    return course_sched

# Generate valid schedules for a string list of courses. First construct a
# a list of components, where a "component" is a set of classes where each
# class contains information such as class time, id, location, etc, and share
# a component if they have the same course id and component such as LEC or
# LAB. Early exit if the SAT solver proves unsatisfiability. If the size of
# possibly valid schedules is within a computational threshold T, then
# attempt to validate all schedules. If the size exceeds the threshold,
# randomly sample from every axis (component) and gather a subset of all
# possibly valid schedules of size T.
def generate_schedules(course_list):
    (components, aliases) = _create_components(course_list)
    cnf = _build_cnf(components)
    if pycosat.solve(cnf) == "UNSAT":
        return []
    cardinality = _cross_prod_cardinality(components)
    print("Cross product cardinality: " + str(cardinality))
    valid_schedules = []
    if cardinality <= EXHAUST_CARDINALITY_THRESHOLD:
        schedules = list(product(*components))
        valid_schedules = _validate_schedules(schedules)
    else:
        sampled_schedules = []
        for _ in range(EXHAUST_CARDINALITY_THRESHOLD):
            sample_sched = []
            for component in components:
                sample_sched.append(choice(component))
            sampled_schedules.append(sample_sched)
        valid_schedules = _validate_schedules(sampled_schedules)
    sorted_schedules = _master_sort(valid_schedules)
    json_schedules = {"schedules": [_json_sched(s._schedule) for s in sorted_schedules]}
    return (json_schedules, aliases)

'''
(s, a) = schedules = generate_schedules(["CMPUT 174", "MATH 117", "MATH 127", "STAT 151", "WRS 101"])
(s, a) = schedules = generate_schedules(["CHEM 101"])
sched = s[0]._schedule

print(course_sched)

#S = (['LAB', 'D26', 'MAIN', None, [(1020, 1190, 'R', None)], 'CMPUT 174', '45438'], ['LEC', 'A6', 'MAIN', None, [(930, 1010, 'TR', None)], 'CMPUT 174', '47558'], ['LEC', 'SA1', 'MAIN', None, [(780, 830, 'R', 'CCIS L1-140'), (600, 650, 'MWF', 'CCIS L1-140')], 'MATH 117', '44640'], ['LEC', 'A1', 'MAIN', None, [(780, 830, 'T', None), (540, 590, 'MWF', 'CAB 235')], 'MATH 127', '53158'], ['LEC', '802', 'ONLINE', None, [(1020, 1200, 'T', None)], 'STAT 151', '45634'], ['SEM', 'A4', 'MAIN', None, [(840, 920, 'TR', 'HC 2-34')], 'WRS 101', '52320'])
#_closeness_evaluate(S)
'''
