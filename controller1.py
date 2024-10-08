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
        self.threshold = 300000 # soglia di throughput (bit/secondo)
        self.watchlist = {}
        self.blocklist = {}
        self.lower_threshold = 0.02 * self.threshold

        self.thread_monitorning = threading.Thread(target=self._monitor)
        self.thread_monitorning.daemon = True
        self.thread_monitorning.start()

        self.thread_mitigation = threading.Thread(target=self._limit_rate)
        self.thread_mitigation.daemon = True
        self.thread_mitigation.start()

    def _monitor(self):
        self.logger.info("Monitor thread started")
        if os.path.exists('port_stats.csv'):
            os.remove('port_stats.csv')
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            self._stats_csv() #?
            time.sleep(2)

    def _limit_rate(self):
        self.logger.info("Mitigation thread started")

        while True:

            for (dpid, port_no), count in list(self.blocklist.items()):
                if count > 4:
                    self._unblock_port(dpid, port_no)
                    del self.blocklist[(dpid, port_no)]
                else:
                    self.blocklist[(dpid, port_no)] += 1

            self.logger.info(f"Blocklist: {self.blocklist}")
            time.sleep(5)

    def _block_port(self, dpid, port_no):
        del self.watchlist[(dpid, port_no)]
        self.logger.info(f'\nRIMUOVO Watchlist: {self.watchlist}\n')
        datapath = self.datapaths[dpid]
        parser = datapath.ofproto_parser

        # Create a match for incoming traffic on the port
        match = parser.OFPMatch(in_port=port_no)

        # Create an action to drop packets
        actions = []

        self.add_flow(datapath, 2, match, actions)

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=0, in_port=port_no, actions=actions, data=None)
        datapath.send_msg(out)

        self.logger.info('Blocking port: Switch %s, Port %s, RX Throughput=%f', dpid, port_no, self.port_stats[dpid][port_no].get('rx_throughput', 0))

    def _unblock_port(self, dpid, port_no):
        self.logger.info('Unblocking port: Switch %s, Port %s', dpid, port_no)

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


            # Skip special port numbers
            if port_no >= ofproto_v1_3.OFPP_MAX:
                continue


            if port_no not in self.port_stats[dpid]:
                self.port_stats[dpid][port_no] = {
                    'rx_bytes': stat.rx_bytes,
                    'tx_bytes': stat.tx_bytes,
                    'timestamp': time.time()
                }
            else:
                prev_stats = self.port_stats[dpid][port_no]
                curr_time = time.time()
                time_diff = curr_time - prev_stats['timestamp']

                rx_throughput = (stat.rx_bytes - prev_stats['rx_bytes']) / time_diff
                tx_throughput = (stat.tx_bytes - prev_stats['tx_bytes']) / time_diff

                #self.logger.info('Switch %s, Porta %s - RX Throughput: %f bytes/sec, TX Throughput: %f bytes/sec', dpid, port_no, rx_throughput, tx_throughput)

                self.active_ports = [p for p in self.port_stats[dpid] if self.port_stats[dpid][p].get('rx_throughput', 0) > self.lower_threshold] #solo porte attive in RICEZIONE
                self.num_active_ports = len(self.active_ports) -  len(self.blocklist) 
                

                if dpid == 3:
                    self.logger.info(f'dpid {dpid}')
                    self.logger.info(f'datapath id {datapath.id}')
                    self.logger.info(f'\nPorte attive in ricezione sullo switch {dpid}: {self.num_active_ports}\n')
                    if rx_throughput > self.threshold: #rimosso controllo porte attive
                        self.logger.warning('Allarme! Switch %s, Porta %s ha superato la soglia con throughput: RX=%f', dpid, port_no, rx_throughput)
                        if (dpid, port_no) not in self.watchlist and (dpid, port_no) not in self.blocklist:
                            self.watchlist[(dpid, port_no)] = 0
                        elif (dpid, port_no) in self.watchlist:
                            self.watchlist[(dpid, port_no)] += 1

                        self.logger.info(f'\n AGGIUNGO Watchlist: {self.watchlist}\n')
                    elif rx_throughput < self.threshold:
                        if (dpid, port_no) in self.watchlist:
                            if self.watchlist[(dpid, port_no)] > 0:
                                self.watchlist[(dpid, port_no)] -= 1

                # Calculate the final threshold based on the number of active ports
                final_threshold = (self.threshold + self.threshold * 0.1) / self.num_active_ports if self.num_active_ports > 0 else 1 #questo else servnel caso si arriva qui con un numero di porte attive =0 , x evitare errori

                if (dpid, port_no) in self.watchlist:
                    if rx_throughput > final_threshold and self.watchlist[(dpid, port_no)] > 1:
                        if (dpid, port_no) not in self.blocklist:
                            self.blocklist[(dpid, port_no)] = 0

                        self._block_port(dpid, port_no)


                self.port_stats[dpid][port_no]['rx_bytes'] = stat.rx_bytes
                self.port_stats[dpid][port_no]['tx_bytes'] = stat.tx_bytes
                self.port_stats[dpid][port_no]['timestamp'] = curr_time
                self.port_stats[dpid][port_no]['rx_throughput'] = rx_throughput
                self.port_stats[dpid][port_no]['tx_throughput'] = tx_throughput

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        if ev.msg.msg_len < ev.msg.total_len:
            self.logger.debug("packet truncated: only %s of %s bytes",
                              ev.msg.msg_len, ev.msg.total_len)
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        dst = eth.dst
        src = eth.src

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})

        #self.logger.info("packet in %s %s %s %s", dpid, src, dst, in_port)

        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                return
            else:
                self.add_flow(datapath, 1, match, actions)
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    def _stats_csv(self):
        file_exists = os.path.isfile('port_stats.csv')
        with open('port_stats.csv', mode='a') as csv_file:
            fieldnames = ['timestamp', 'dpid', 'port_no', 'rx_bytes', 'tx_bytes', 'rx_throughput', 'tx_throughput', 'num_active_ports', 'active_ports', 'blocklist']
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)

            if not file_exists:
                writer.writeheader()

            for dpid, ports in self.port_stats.items():
                for port_no, stats in ports.items():
                    human_readable_timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stats['timestamp']))
                    writer.writerow({
                        'timestamp': human_readable_timestamp,
                        'dpid': dpid,
                        'port_no': port_no,
                        'rx_bytes': stats['rx_bytes'],
                        'tx_bytes': stats['tx_bytes'],
                        'rx_throughput': stats.get('rx_throughput', 0),
                        'tx_throughput': stats.get('tx_throughput', 0),
                        'num_active_ports': self.num_active_ports,
                        'active_ports': self.active_ports,
                        'blocklist': self.blocklist
                    })