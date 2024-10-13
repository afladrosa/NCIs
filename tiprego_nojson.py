from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
import threading
import time
import csv
import os

class SimpleSwitch13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SimpleSwitch13, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.num_active_ports = 0
        self.active_ports = []
        self.datapaths = {}
        self.port_stats = {}
        self.threshold = 300000  # Threshold for throughput (bits/second)
        self.lower_threshold = 0.02 * self.threshold
        self.monitoring_list = []
        self.blocked_ports = {}
        self.host_info = {}  # Dictionary to track hosts, their ports, and switches

        # Start the monitoring and mitigation thread
        self.thread_monitoring_mitigation = threading.Thread(target=self._monitor_and_mitigate)
        self.thread_monitoring_mitigation.daemon = True
        self.thread_monitoring_mitigation.start()

    def _monitor_and_mitigate(self):
        self.logger.info("Monitor and Mitigate thread started")
        if os.path.exists('port_stats.csv'):
            os.remove('port_stats.csv')
        while True:
            # Monitoring section
            for dp in self.datapaths.values():
                self._request_stats(dp)
            self._stats_csv()

            # Mitigation section
            for (dpid, port_no), block_time in list(self.blocked_ports.items()):
                if (time.time() - block_time) > 30:
                    self._unblock_port(dpid, port_no)
                    del self.blocked_ports[(dpid, port_no)]
            self.logger.info(f"\n____CURRENTLY BLOCKED PORTS: {list(self.blocked_ports.keys())}____")
            time.sleep(2)

    def _block_port(self, dpid, port_no):
        self.monitoring_list.remove((dpid, port_no))
        datapath = self.datapaths[dpid]
        parser = datapath.ofproto_parser

        # Create a match for incoming traffic on the port
        match = parser.OFPMatch(in_port=port_no)
        actions = []  # No actions mean drop packets

        self.add_flow(datapath, 2, match, actions)

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=0, in_port=port_no, actions=actions, data=None)
        datapath.send_msg(out)

        self.logger.info(f'\n\n*************PORT {(dpid, port_no)} REMOVED FROM monitoring_list AND BLOCKED*************')

    def _unblock_port(self, dpid, port_no):
        self.logger.info(f'\n\n*************PORT {(dpid, port_no)} UNBLOCKED*************')

        # Remove the flow entry that drops packets
        datapath = self.datapaths[dpid]
        parser = datapath.ofproto_parser
        match = parser.OFPMatch(in_port=port_no)
        self.remove_flow(datapath, match)

    def remove_flow(self, datapath, match):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match
        )
        datapath.send_msg(mod)

    def _request_stats(self, datapath):
        self.logger.debug('send stats request: %016x', datapath.id)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, CONFIG_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                self.logger.info('Register datapath: %016x', datapath.id)
                self.datapaths[datapath.id] = datapath
        elif ev.state == 'DEAD_DISPATCHER':
            if datapath.id in self.datapaths:
                self.logger.info('Unregister datapath: %016x', datapath.id)
                del self.datapaths[datapath.id]

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        body = ev.msg.body
        datapath = ev.msg.datapath
        dpid = datapath.id

        self.port_stats.setdefault(dpid, {})

        for stat in body:
            port_no = stat.port_no
            if port_no >= ofproto_v1_3.OFPP_MAX:
                continue

            # Update port stats
            self._update_port_stats(dpid, port_no, stat)
            self._monitor_port(dpid, port_no)

    def _update_port_stats(self, dpid, port_no, stat):
        curr_time = time.time()

        if port_no not in self.port_stats[dpid]:
            self.port_stats[dpid][port_no] = {
                'rx_bytes': stat.rx_bytes,
                'tx_bytes': stat.tx_bytes,
                'timestamp': curr_time
            }
        else:
            prev_stats = self.port_stats[dpid][port_no]
            time_diff = curr_time - prev_stats['timestamp']

            # Throughput calculation
            rx_throughput = (stat.rx_bytes - prev_stats['rx_bytes']) / time_diff
            tx_throughput = (stat.tx_bytes - prev_stats['tx_bytes']) / time_diff

            # Update statistics
            self.port_stats[dpid][port_no]['rx_bytes'] = stat.rx_bytes
            self.port_stats[dpid][port_no]['tx_bytes'] = stat.tx_bytes
            self.port_stats[dpid][port_no]['timestamp'] = curr_time
            self.port_stats[dpid][port_no]['rx_throughput'] = rx_throughput
            self.port_stats[dpid][port_no]['tx_throughput'] = tx_throughput

    def _monitor_port(self, dpid, port_no):
        rx_throughput = self.port_stats[dpid][port_no].get('rx_throughput', 0)

        # Get active ports
        self.active_ports = [p for p in self.port_stats[dpid] if self.port_stats[dpid][p].get('rx_throughput', 0) > self.lower_threshold]
        self.num_active_ports = len(self.active_ports) - len(self.blocked_ports)

        # Monitor ports based on throughput
        if rx_throughput > self.threshold:
            if ((dpid, port_no) not in self.monitoring_list and (dpid, port_no) not in self.blocked_ports):
                self.logger.warning(f'\n*************PORT {(dpid, port_no)} EXCEEDED THRESHOLD WITH RX=%f*************', rx_throughput)
                # Add host info dynamically
                if (dpid, port_no) not in self.host_info:  # Check if it's not already in host_info
                    self.host_info[(dpid, port_no)] = {'status': 'active'}  # Track port status
                    self.monitoring_list.append((dpid, port_no))
                    self.logger.info(f'\n*************PORT {(dpid, port_no)} ADDED TO monitoring_list: {self.monitoring_list}*************')
            elif (dpid, port_no) in self.monitoring_list and (dpid, port_no) not in self.blocked_ports:
                self.logger.warning(f'\n*************PORT {(dpid, port_no)} EXCEEDED THRESHOLD WITH RX=%f*************', rx_throughput)
                self.blocked_ports[(dpid, port_no)] = time.time()
                self._block_port(dpid, port_no)

        elif rx_throughput < self.threshold:
            if (dpid, port_no) in self.monitoring_list:
                self.monitoring_list.remove((dpid, port_no))
                self.logger.info(f'\n\n*************PORT {(dpid, port_no)} REMOVED FROM monitoring_list -> Current monitoring_list: {self.monitoring_list}*************')

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Install a flow to send packets from the switch to the controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

        self.logger.info("Switch features installed")

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        if buffer_id:
            inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
            flow_mod = parser.OFPFlowMod(datapath=datapath, priority=priority, buffer_id=buffer_id,
                                          match=match, instructions=inst)
            datapath.send_msg(flow_mod)
        else:
            flow_mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match,
                                          instructions=[parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)])
            datapath.send_msg(flow_mod)

    @set_ev_cls(ofp_event.EventOFPPortStatus, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def port_status_handler(self, ev):
        port = ev.port
        dpid = ev.datapath.id
        port_no = port.port_no

        if ev.status == ev.OFPPRADD:
            self.logger.info(f'Port {port_no} added to switch {dpid}')
            self.host_info[(dpid, port_no)] = {'status': 'active'}
        
        elif ev.status == ev.OFPPRDELETE:
            self.logger.info(f'Port {port_no} removed from switch {dpid}')
            if (dpid, port_no) in self.host_info:
                del self.host_info[(dpid, port_no)]
        
        elif ev.status == ev.OFPPRMODIFY:
            self.logger.info(f'Port {port_no} modified on switch {dpid}')

    @set_ev_cls(ofp_event.EventOFPTableStatsReply, MAIN_DISPATCHER)
    def table_stats_reply_handler(self, ev):
        self.logger.info("Table stats received.")
        # Here you can handle table stats if needed.
