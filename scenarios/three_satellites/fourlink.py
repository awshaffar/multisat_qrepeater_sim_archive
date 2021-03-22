import os, sys; sys.path.insert(0, os.path.abspath("."))
import numpy as np
from world import World
from protocol import Protocol
from events import SourceEvent, EntanglementSwappingEvent
import libs.matrix as mat
from libs.aux_functions import apply_single_qubit_map, y_noise_channel, z_noise_channel, w_noise_channel, distance
from consts import ETA_ATM_PI_HALF_780_NM
from consts import AVERAGE_EARTH_RADIUS as R_E
from consts import SPEED_OF_LIGHT_IN_VACCUM as C
from functools import lru_cache
from noise import NoiseModel, NoiseChannel
from quantum_objects import SchedulingSource, Station


def construct_dephasing_noise_channel(dephasing_time):
    def lambda_dp(t):
        return (1 - np.exp(-t / dephasing_time)) / 2

    def dephasing_noise_channel(rho, t):
        return z_noise_channel(rho=rho, epsilon=lambda_dp(t))

    return dephasing_noise_channel


def construct_y_noise_channel(epsilon):
    return lambda rho: y_noise_channel(rho=rho, epsilon=epsilon)


def construct_w_noise_channel(epsilon):
    return lambda rho: w_noise_channel(rho=rho, alpha=(1 - epsilon))


def alpha_of_eta(eta, p_d):
    return eta * (1 - p_d) / (1 - (1 - eta) * (1 - p_d)**2)


def eta_dif(distance, divergence_half_angle, sender_aperture_radius, receiver_aperture_radius):
    # calculated by simple geometry, because gaussian effects do not matter much
    x = sender_aperture_radius + distance * np.tan(divergence_half_angle)
    arriving_fraction = receiver_aperture_radius**2 / x**2
    if arriving_fraction > 1:
        arriving_fraction = 1
    return arriving_fraction


def eta_atm(elevation):
    # eta of pi/2 to the power of csc(theta), equation (A4) in https://arxiv.org/abs/2006.10636
    # eta of pi/2 (i.e. straight up) is ~0.8 for 780nm wavelength.
    if elevation < 0:
        return 0
    return ETA_ATM_PI_HALF_780_NM**(1 / np.sin(elevation))


def sat_dist_curved(ground_dist, h):
    # ground dist refers to distance between station and the "shadow" of the satellite
    alpha = ground_dist / R_E
    L = np.sqrt(R_E**2 + (R_E + h)**2 - 2 * R_E * (R_E + h) * np.cos(alpha))
    return L


def elevation_curved(ground_dist, h):
    # ground dist refers to distance between station and the "shadow" of the satellite
    alpha = ground_dist / R_E
    L = np.sqrt(R_E**2 + (R_E + h)**2 - 2 * R_E * (R_E + h) * np.cos(alpha))
    beta = np.arcsin(R_E / L * np.sin(alpha))
    gamma = np.pi - alpha - beta
    return gamma - np.pi / 2

class FourlinkProtocol(Protocol):
    #
    def __init__(self, world, num_memories, stations, sources):
        self.num_memories = num_memories
        self.time_list = []
        self.state_list = []
        self.resource_cost_max_list = []
        self.resource_cost_add_list = []
        self.stations = stations
        self.sources = sources
        super(FourlinkProtocol, self).__init__(world=world)

    def setup(self):
        assert len(self.stations) == 5
        self.station_ground_left = self.stations[0]
        self.sat_left = self.stations[1]
        self.sat_central = self.stations[2]
        self.sat_right = self.stations[3]
        self.station_ground_right = self.stations[4]
        assert len(self.sources) == 4
        self.source_link1, self.source_link2, self.source_link3, self.source_link4 = self.sources
        for source in self.sources:
            assert callable(getattr(source, "schedule_event", None))# schedule_event is a required method for this protocol
        self.link_stations = [[self.stations[i], self.stations[i+1]] for i in range(4)]

    def _get_pairs_between_stations(self, station1, station2):
        try:
            pairs = self.world.world_objects["Pair"]
        except KeyError:
            pairs = []
        return list(filter(lambda pair: pair.is_between_stations(station1, station2), pairs))

    def _get_pairs_scheduled(self, station1, station2):
        return list(filter(lambda event: (isinstance(event, SourceEvent)
                           and (station1 in event.source.target_stations)
                           and (station2 in event.source.target_stations)
                           ),
                    self.world.event_queue.queue))

    def _eval_pair(self, long_range_pair):
        comm_distance = np.max([distance(self.sat_central, self.station_ground_left), distance(self.sat_central, self.station_ground_right)])
        comm_time = comm_distance / C

        self.time_list += [self.world.event_queue.current_time + comm_time]
        self.state_list += [long_range_pair.state]
        self.resource_cost_max_list += [long_range_pair.resource_cost_max]
        self.resource_cost_add_list += [long_range_pair.resource_cost_add]
        return

    # def check(self):
    #     pairs_link1 = self._get_pairs_between_stations(self.station_ground_left,self.sat_left)
    #     num_pairs_link1 = len(pairs_link1)
    #     num_pairs_scheduled_link1 = len(self._get_pairs_scheduled(self.station_ground_left,self.sat_left))
    #     link1_used = num_pairs_link1 + num_pairs_scheduled_link1
    #
    #     if pairs_link1 == [] and num_pairs_scheduled_link1 == 0:
    #         for _ in range(self.num_memories - link1_used):
    #             self.source_link1.schedule_event()

    def check(self):
        pairs_links = [self._get_pairs_between_stations(*stations) for stations in self.link_stations]
        num_pairs_links = [len(pairs_link) for pairs_link in pairs_links]
        num_pairs_scheduled_links = [len(self._get_pairs_scheduled(*stations)) for stations in self.link_stations]
        links_used = [num_pairs_link + num_pairs_scheduled_link for num_pairs_link, num_pairs_scheduled_link in zip(num_pairs_links, num_pairs_scheduled_links)]
        for link_used, source in zip(links_used, self.sources):
            if link_used < self.num_memories:
                for _ in range(self.num_memories - link_used):
                    source.schedule_event()
        #Swapping left:
        if num_pairs_links[0] != 0 and num_pairs_links[1] != 0:
            num_swappings = min(num_pairs_links[0], num_pairs_links[1])
            for left_pair, right_pair in zip(pairs_links[0], pairs_links[1]):
                # assert that we do not schedule the same swapping more than once
                try:
                    next(filter(lambda event: (isinstance(event, EntanglementSwappingEvent)
                                               and (left_pair in event.pairs)
                                               and (right_pair in event.pairs)
                                               ),
                                self.world.event_queue.queue))
                    is_already_scheduled = True
                except StopIteration:
                    is_already_scheduled = False
                if not is_already_scheduled:
                    ent_swap_event = EntanglementSwappingEvent(time=self.world.event_queue.current_time, pairs=[left_pair, right_pair])
                    self.world.event_queue.add_event(ent_swap_event)
        #Swapping right:
        if num_pairs_links[2] != 0 and num_pairs_links[3] != 0:
            num_swappings = min(num_pairs_links[2], num_pairs_links[3])
            for left_pair, right_pair in zip(pairs_links[2], pairs_links[3]):
                # assert that we do not schedule the same swapping more than once
                try:
                    next(filter(lambda event: (isinstance(event, EntanglementSwappingEvent)
                                               and (left_pair in event.pairs)
                                               and (right_pair in event.pairs)
                                               ),
                                self.world.event_queue.queue))
                    is_already_scheduled = True
                except StopIteration:
                    is_already_scheduled = False
                if not is_already_scheduled:
                    ent_swap_event = EntanglementSwappingEvent(time=self.world.event_queue.current_time, pairs=[left_pair, right_pair])
                    self.world.event_queue.add_event(ent_swap_event)
        #Swapping central:
        left_pairs = self._get_pairs_between_stations(self.station_ground_left, self.sat_central)
        right_pairs = self._get_pairs_between_stations(self.sat_central, self.station_ground_right)
        num_left_pairs = len(left_pairs)
        num_right_pairs = len(right_pairs)
        if num_left_pairs!= 0 and num_right_pairs != 0:
            num_swappings = min(num_left_pairs, num_right_pairs)
            for left_pair, right_pair in zip(left_pairs, right_pairs):
                # assert that we do not schedule the same swapping more than once
                try:
                    next(filter(lambda event: (isinstance(event, EntanglementSwappingEvent)
                                               and (left_pair in event.pairs)
                                               and (right_pair in event.pairs)
                                               ),
                                self.world.event_queue.queue))
                    is_already_scheduled = True
                except StopIteration:
                    is_already_scheduled = False
                if not is_already_scheduled:
                    ent_swap_event = EntanglementSwappingEvent(time=self.world.event_queue.current_time, pairs=[left_pair, right_pair])
                    self.world.event_queue.add_event(ent_swap_event)
        #Evaluate long range pairs
        long_range_pairs = self._get_pairs_between_stations(self.station_ground_left, self.station_ground_right)
        if long_range_pairs:
            for long_range_pair in long_range_pairs:
                self._eval_pair(long_range_pair)
                # cleanup
                long_range_pair.qubits[0].destroy()
                long_range_pair.qubits[1].destroy()
                long_range_pair.destroy()
            self.check()

if __name__ == "__main__":
    length = 200e3
    P_LINK = 1
    T_P = 0
    T_DP = 1.0
    E_MA = 0
    P_D = 0
    LAMBDA_BSM = 1
    ORBITAL_HEIGHT = 400e3
    SENDER_APERTURE_RADIUS = 0.15
    RECEIVER_APERTURE_RADIUS = 0.50
    DIVERGENCE_THETA = 1e-6
    first_satellite_ground_dist_multiplier = 0.25

    def position_from_angle(radius, angle):
        return radius * np.array([np.sin(angle), np.cos(angle)])

    station_a_angle = 0
    station_a_position = position_from_angle(R_E, station_a_angle)
    first_satellite_angle = first_satellite_ground_dist_multiplier * length / R_E
    first_satellite_position = position_from_angle(R_E + ORBITAL_HEIGHT, first_satellite_angle)
    second_satellite_angle = length / 2 / R_E
    second_satellite_position = position_from_angle(R_E + ORBITAL_HEIGHT, second_satellite_angle)
    third_satellite_angle = (1 - first_satellite_ground_dist_multiplier) * length / R_E
    third_satellite_position = position_from_angle(R_E + ORBITAL_HEIGHT, third_satellite_angle)
    station_b_angle = length / R_E
    station_b_position = position_from_angle(R_E, station_b_angle)
    elevation_left = elevation_curved(ground_dist=first_satellite_ground_dist_multiplier * length, h=ORBITAL_HEIGHT)
    elevation_right = elevation_curved(ground_dist=first_satellite_ground_dist_multiplier * length, h=ORBITAL_HEIGHT)
    arrival_chance_link1 = eta_atm(elevation_left) \
                          * eta_dif(distance=distance(station_a_position, first_satellite_position),
                                    divergence_half_angle=DIVERGENCE_THETA,
                                    sender_aperture_radius=SENDER_APERTURE_RADIUS,
                                    receiver_aperture_radius=RECEIVER_APERTURE_RADIUS)
    arrival_chance_link2 = eta_dif(distance=distance(first_satellite_position, second_satellite_position),
                                   divergence_half_angle=DIVERGENCE_THETA,
                                   sender_aperture_radius=SENDER_APERTURE_RADIUS,
                                   receiver_aperture_radius=RECEIVER_APERTURE_RADIUS)
    arrival_chance_link3 = eta_dif(distance=distance(second_satellite_position, third_satellite_position),
                                   divergence_half_angle=DIVERGENCE_THETA,
                                   sender_aperture_radius=SENDER_APERTURE_RADIUS,
                                   receiver_aperture_radius=RECEIVER_APERTURE_RADIUS)
    arrival_chance_link4 = eta_atm(elevation_right) \
                          * eta_dif(distance=distance(third_satellite_position, station_b_position),
                                    divergence_half_angle=DIVERGENCE_THETA,
                                    sender_aperture_radius=SENDER_APERTURE_RADIUS,
                                    receiver_aperture_radius=RECEIVER_APERTURE_RADIUS)

    def generate_time_distribution(arrival_chance):
        def time_distribution(source):
            comm_distance = np.max([distance(source, source.target_stations[0]), distance(source.target_stations[1], source)])
            comm_time = 2 * comm_distance / C
            eta = P_LINK * arrival_chance
            eta_effective = 1 - (1 - eta) * (1 - P_D)**2
            trial_time = T_P + comm_time  # I don't think that paper uses latency time and loading time?
            random_num = np.random.geometric(eta_effective)
            return random_num * trial_time, random_num
        return time_distribution

    time_distribution_link1 = generate_time_distribution(arrival_chance_link1)
    time_distribution_link2 = generate_time_distribution(arrival_chance_link2)
    time_distribution_link3 = generate_time_distribution(arrival_chance_link3)
    time_distribution_link4 = generate_time_distribution(arrival_chance_link4)

    def generate_state_generation(arrival_chance):
        def state_generation(source):
            state = np.dot(mat.phiplus, mat.H(mat.phiplus))
            comm_distance = np.max([distance(source, source.target_stations[0]), distance(source.target_stations[1], source)])
            storage_time = 2 * comm_distance / C
            for idx, station in enumerate(source.target_stations):
                if station.memory_noise is not None:  # dephasing that has accrued while other qubit was travelling
                    state = apply_single_qubit_map(map_func=station.memory_noise, qubit_index=idx, rho=state, t=storage_time)
                if station.dark_count_probability is not None:  # dark counts are handled here because the information about eta is needed for that
                    eta = P_LINK * arrival_chance
                    state = apply_single_qubit_map(map_func=w_noise_channel, qubit_index=idx, rho=state, alpha=alpha_of_eta(eta=eta, p_d=station.dark_count_probability))
            return state
        return state_generation

    state_generation_link1 = generate_state_generation(arrival_chance_link1)
    state_generation_link2 = generate_state_generation(arrival_chance_link2)
    state_generation_link3 = generate_state_generation(arrival_chance_link3)
    state_generation_link4 = generate_state_generation(arrival_chance_link4)

    world = World()

    station_ground_left = Station(world, position = station_a_position)
    station_sat_left = Station(world, position = first_satellite_position)
    station_sat_central = Station(world, position = second_satellite_position)
    station_sat_right = Station(world, position = third_satellite_position)
    station_ground_right = Station(world, position = station_b_position)

    source_sat_left1 = SchedulingSource(world, position = first_satellite_position, target_stations = [station_ground_left, station_sat_left], time_distribution = time_distribution_link1, state_generation = state_generation_link1)
    source_sat_left2 = SchedulingSource(world, position = first_satellite_position, target_stations = [station_sat_left, station_sat_central], time_distribution = time_distribution_link2, state_generation = state_generation_link2)
    source_sat_right1 = SchedulingSource(world, position = third_satellite_position, target_stations = [station_sat_central, station_sat_right], time_distribution = time_distribution_link3, state_generation = state_generation_link3)
    source_sat_right2 = SchedulingSource(world, position = third_satellite_position, target_stations = [station_sat_right, station_ground_right], time_distribution = time_distribution_link4, state_generation = state_generation_link4)

    protocol = FourlinkProtocol(world, num_memories = 2, stations = [station_ground_left, station_sat_left, station_sat_central, station_sat_right, station_ground_right], sources = [source_sat_left1 , source_sat_left2, source_sat_right1, source_sat_right2])
    protocol.setup()

    while len(protocol.time_list) < 100:
        protocol.check()
        world.event_queue.resolve_next_event()
