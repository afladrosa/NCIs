from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
from ryu.topology import event
from ryu.topology.api import get_switch, get_link, get_host
import threading
import time
import csv
import os

class SimpleSwitch13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SimpleSwitch13, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.host_info = {}  # dizionario per tracciare gli host dinamicamente
        self.thread_monitoring_mitigation = threading.Thread(target=self._monitor_and_mitigate)
        self.thread_monitoring_mitigation.daemon = True
        self.thread_monitoring_mitigation.start()

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
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
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

        # Memorizza la porta di ingresso
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

    # --- Funzionalità dinamiche per la gestione della topologia ---
    
    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        self.logger.info("Switch connesso: %s", ev.switch.dp.id)
        self.update_topology()

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        self.logger.info("Switch disconnesso: %s", ev.switch.dp.id)
        self.update_topology()

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        self.logger.info("Link aggiunto tra: %s <-> %s", ev.link.src, ev.link.dst)
        self.update_topology()

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        self.logger.info("Link rimosso tra: %s <-> %s", ev.link.src, ev.link.dst)
        self.update_topology()

    @set_ev_cls(event.EventHostAdd)
    def host_add_handler(self, ev):
        host = ev.host
        self.logger.info("Host rilevato: %s su porta %s dello switch %s",
                         host.mac, host.port.port_no, host.port.dpid)
        self.host_info[host.mac] = (host.port.dpid, host.port.port_no)
        self.logger.info(f"Host info aggiornata: {self.host_info}")

    def update_topology(self):
        self.logger.info("Aggiornamento topologia...")
        switches = get_switch(self, None)
        links = get_link(self, None)
        self.logger.info("Switches: %s", [switch.dp.id for switch in switches])
        self.logger.info("Links: %s", [(link.src, link.dst) for link in links])

    def _monitor_and_mitigate(self):
        self.logger.info("Monitor and Mitigate thread started")
        if os.path.exists('port_stats.csv'):
            os.remove('port_stats.csv')
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            self._stats_csv()
            time.sleep(2)

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
    
            # Chiama la funzione per aggiornare le statistiche della porta
            self._update_port_stats(dpid, port_no, stat)
    
            # Chiama la funzione per monitorare la porta
            self._monitor_port(dpid, port_no)
    
    def _update_port_stats(self, dpid, port_no, stat):
        curr_time = time.time()
    
        # Inizializza le statistiche se la porta non è presente
        if port_no not in self.port_stats[dpid]:
            self.port_stats[dpid][port_no] = {
                'rx_bytes': stat.rx_bytes,
                'tx_bytes': stat.tx_bytes,
                'timestamp': curr_time
            }
        else:
            prev_stats = self.port_stats[dpid][port_no]
            time_diff = curr_time - prev_stats['timestamp']
    
            # Calcolo del throughput
            rx_throughput = (stat.rx_bytes - prev_stats['rx_bytes']) / time_diff
            tx_throughput = (stat.tx_bytes - prev_stats['tx_bytes']) / time_diff
    
            # Aggiornamento delle statistiche
            self.port_stats[dpid][port_no]['rx_bytes'] = stat.rx_bytes
            self.port_stats[dpid][port_no]['tx_bytes'] = stat.tx_bytes
            self.port_stats[dpid][port_no]['timestamp'] = curr_time
            self.port_stats[dpid][port_no]['rx_throughput'] = rx_throughput
            self.port_stats[dpid][port_no]['tx_throughput'] = tx_throughput
    
    def _monitor_port(self, dpid, port_no):
        rx_throughput = self.port_stats[dpid][port_no].get('rx_throughput', 0)
    
        self.active_ports = [p for p in self.port_stats[dpid] if self.port_stats[dpid][p].get('rx_throughput', 0) > self.lower_threshold]
        self.num_active_ports = len(self.active_ports) - len(self.blocked_ports)
    
            # Monitoraggio delle porte in base al throughput
        if rx_throughput > self.threshold:
            if ((dpid, port_no) not in self.monitoring_list and (dpid, port_no) not in self.blocked_ports):
                
                self.logger.warning(f'\n*************LA PORTA {(dpid, port_no)} HA SUPERATO LA SOGLIA CON RX=%f*************', rx_throughput)
                if (dpid,port_no) in self.host_info:
                    self.monitoring_list.append((dpid, port_no))
                    self.logger.info(f'\n*************PORTA {(dpid, port_no)} AGGIUNTA ALLA monitoring_list: {self.monitoring_list}*************')
                else:
                    self.logger.info(f'\nLA PORTA {(dpid, port_no)} È ATTRAVERSATA DA TRAFFICO INTERMEDIO -> NON AGGIUNTA ALLA monitoring_list')
            elif (dpid, port_no) in self.monitoring_list and (dpid, port_no) not in self.blocked_ports and (dpid,port_no) in self.host_info:
                self.logger.warning(f'\n*************LA PORTA {(dpid, port_no)} HA SUPERATO LA SOGLIA CON RX=%f*************', rx_throughput)
                self.blocked_ports[(dpid, port_no)] = time.time()
                self._block_port(dpid, port_no)
    
        elif rx_throughput < self.threshold:
            if (dpid, port_no) in self.monitoring_list:
                self.monitoring_list.remove((dpid, port_no))
                self.logger.info(f'\n\n*************PORTA {(dpid, port_no)} RIMOSSA DALLA monitoring_list -> monitoring_list attuale: {self.monitoring_list}*************')
  
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
            fieldnames = ['timestamp', 'dpid', 'port_no', 'rx_bytes', 'tx_bytes', 'rx_throughput', 'tx_throughput', 'num_active_ports', 'active_ports', 'blocked_ports']
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
                        'blocked_ports': self.blocked_ports
                    })
