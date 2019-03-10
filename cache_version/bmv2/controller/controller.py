#!/usr/bin/env python
# coding=utf-8

from scapy.all import *
from data_structure import IP2HC, TCP_Session
from config import *
from switch import NetHCFSwitchBMv2
import time
from multiprocessing import Process, Manager

class NetHCFController:
    def __init__(self, iface, default_hc_list):
        self.switch = NetHCFSwitchBMv2(
            NETHCF_SWITCH_CONFIG, TARGET_THRIFT_IP, TARGET_THRIFT_PORT
        )
        # MultiProcessing Manager Instance for memory sharing
        self.mpmgr = Manager()
        self.iface = iface
        self.ip2hc = IP2HC(impact_factor_function, default_hc_list, self.mpmgr);
        self.tcp_session = TCP_Session()
        self.miss = self.mpmgr.Value('I', 0)
        self.mismatch = self.mpmgr.Value('I', 0)
        self.hcf_state = self.mpmgr.Value('B', 0) # 0: learning 1: filtering
        self.learn_to_filter_thr = LEARN_TO_FILTER_THR
        self.filter_to_learn_thr = FILTER_TO_LEARN_THR

    def initialize(self):
        self.hcf_state.value = 0
        self.switch.switch_to_learning_state()
        self.load_cache_into_switch()
        self.reset_period_counters()

    def run(self):
        self.initialize()
        self.process_packets()

    def run_parallel(self):
        self.initialize()
        update_process = Process(target=self.process_updates)
        packet_process = Process(target=self.process_packets)
        update_process.start()
        packet_process.start()
        update_process.join()
        packet_process.join()

    def process_packets(self):
        sniff(iface=self.iface, prn=self.packets_callback())

    # Nested function for passing "self" parameter to sniff's callback function
    def packets_callback(self):
        def process_function(pkt):
            if pkt[Ether].type != TYPE_IPV4:
                # This is not a IPv4 packet, ignore it temporarily
                return
            # This is a IPv4 packet
            if pkt[IP].dst == CONTROLLER_IP:
                # This is update request
                if pkt[IP].proto == TYPE_TCP:
                    # This is a write back request
                    # A SYN ACK ACK packet with replaced dst address
                    self.ip2hc.update(
                        pkt[IP].src,
                        self.compute_hc(pkt[IP])
                    )
                elif pkt[IP].proto == TYPE_NETHCF:
                    # This is a cache update request
                    self.process_update_request()
            else:
                # This is the header of traffic missing IP2HC in the cache
                self.process_packets_miss_cache(pkt)
        return process_function

    def compute_hc(self, current_ttl):
        hop_count = 0
        hop_count_possible = 0
        # Select initial TTL according to current TTL, and compute HC
        if 0 <= current_ttl <= 29:
            # Initial TTL may be 30, or 32
            hop_count = 30 - current_ttl
            hop_count_possible = 32 - current_ttl
        elif 30 <= current_ttl <= 31:
            hop_count = 32 - current_ttl
            hop_count_possible = hop_count
        elif 32 <= current_ttl <= 59:
            # Initial TTL may be 60, or 64
            hop_count = 60 - current_ttl
            hop_count_possible = 64 - current_ttl
        elif 60 <= current_ttl <= 63:
            hop_count = 64 - current_ttl
            hop_count_possible = hop_count
        elif 64 <= current_ttl <= 127:
            hop_count = 128 - current_ttl
            hop_count_possible = hop_count
        else:
            hop_count = 255 - current_ttl
            hop_count_possible = hop_count
        return hop_count, hop_count_possible

    def process_packets_miss_cache(self, pkt):
        # # Temporary method
        # pkt[IP].src = pkt[IP].src.replace("10", "0", 1)
        # pkt[IP].dst = pkt[IP].dst.replace("10", "0", 1)
        if DEBUG_OPTION:
            print "Debug: " + pkt.summary()
        hc_in_ip2hc = self.ip2hc.read(pkt[IP].src)
        hop_count, hop_count_possible = self.compute_hc(pkt[IP].ttl)
        if hop_count==hc_in_ip2hc or hop_count_possible==hc_in_ip2hc:
            # Update IP2HC match statistics
            if pkt[IP].proto == TYPE_TCP and \
               pkt[TCP].flags == (FLAG_SYN | FLAG_ACK):
                self.tcp_session.update(pkt[IP].dst, 1, pkt[TCP].seq)
            else:
                self.ip2hc.hit_in_controller(pkt[IP].src, 1)
            if self.hcf_state.value == 1:
                sendp(pkt, iface=self.iface)
        else:
            # The HC may not be computed,
            # or the HC should be updated,
            # or this is an abnormal packet
            if pkt[IP].proto != TYPE_TCP:
                # However, we think it is abnormal traffic
                self.mismatch.value += 1
                return
            if pkt[TCP].flags == (FLAG_SYN | FLAG_ACK):
                self.tcp_session.update(pkt[IP].dst, 1, pkt[TCP].seq)
            elif pkt[TCP].flags == FLAG_ACK:
                state, seq_no = self.tcp_session.read(pkt[IP].src)
                # This is SYN ACK ACK.
                if state == 1 and pkt[TCP].ack == seq_no + 1:
                    # The connection is established
                    self.tcp_session.update(pkt[IP].src, 0, 0)
                    self.ip2hc.update(pkt[IP].src, hop_count)
                    # SYN, SYN ACK ACK, total two times for ip_addr(src)
                    self.ip2hc.hit_in_controller(pkt[IP].src, 2)
                    # Eliminate the effect of SYN
                    self.mismatch.value -= 1
                else:
                    # Abnormal packet
                    self.mismatch.value += 1
            else:
                # Such as SYN
                self.mismatch.value += 1

    def process_updates(self):
        while True:
            time.sleep(UPDATE_PERIOD)
            self.process_update_request()

    def process_update_request(self):
        self.pull_switch_counters()
        # Switch state in terms of abnormal_counter in last period
        if self.hcf_state.value == 0 and \
           self.mismatch.value > self.learn_to_filter_thr:
            self.hcf_state.value = 1
            self.switch.switch_to_filtering_state()
        elif self.hcf_state.value == 1 and \
             self.mismatch.value < self.filter_to_learn_thr:
            self.hcf_state.value = 0
            self.switch.switch_to_learning_state()
        elif self.hcf_state.value == 0:
            update_scheme = self.ip2hc.update_cache(self.miss.value)
            self.update_cache_into_switch(update_scheme)
        self.reset_period_counters()

    # Assume controller is running on the switch
    def pull_switch_counters(self):
        self.miss.value = self.switch.read_miss_counter()
        self.mismatch.value += self.switch.read_mismatch_counter()
        self.switch.read_hits_bitmap()
        for idx in range(self.ip2hc.get_cached_size()):
            self.ip2hc.sync_match_times(idx, self.switch.read_hits_counter(idx))

    def load_cache_into_switch(self):
        for idx in range(self.ip2hc.get_cached_size()):
            ip_addr, hc_value = self.ip2hc.get_cached_info(idx)
            entry_handle = self.switch.add_into_ip2hc_mat(ip_addr, idx)
            if entry_handle != -1:
                self.ip2hc.update_entry_handle_in_cache(idx, entry_handle)
                self.switch.update_hc_value(idx, hc_value)

    def update_cache_into_switch(self, update_scheme):
        for cache_idx in update_scheme.keys():
            entry_handle = update_scheme[cache_idx][0]
            new_ip_addr = update_scheme[cache_idx][1]
            hc_value = update_scheme[cache_idx][2]
            if entry_handle != NOT_DELETE_HANDLE:
                self.switch.delete_from_ip2hc_mat(entry_handle)
            entry_handle = self.switch.add_into_ip2hc_mat(new_ip_addr,cache_idx)
            if entry_handle != -1:
                self.ip2hc.update_entry_handle_in_cache(cache_idx, entry_handle)
                self.switch.update_hc_value(cache_idx, hc_value)

    def reset_period_counters(self):
        self.miss.value = 0
        self.mismatch.value = 0
        self.switch.reset_miss_counter()
        self.switch.reset_mismatch_counter()
        self.switch.reset_hits_counter()
        self.switch.reset_hits_bitmap()
        self.ip2hc.reset_last_matched()

if __name__ == "__main__":
    controller = NetHCFController("s1-eth3", {0x0A00000B: 64})
    # controller.run()
    controller.run_parallel()
