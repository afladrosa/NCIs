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
        self.threshold = 300000  # soglia di throughput (bit/secondo)
        self.lower_threshold = 0.02 * self.threshold
        self.monitoring_list = []   
        self.blocked_ports = {}
        self.host_info = {}  # dizionario per tenere traccia degli host e delle loro porte e switch

        self.thread_monitoring_mitigation = threading.Thread(target=self._monitor_and_mitigate)  # unico thread che fa sia monitoring che mitigation
        self.thread_monitoring_mitigation.daemon = True
        self.thread_monitoring_mitigation.start()

    # Funzione per aggiornare le informazioni sugli host
    def _update_host_info(self, dpid, port_no, src_mac):
        if dpid not in self.host_info:
            self.host_info[dpid] = {}
        self.host_info[dpid][src_mac] = port_no
        print("***HOST****\n")
        print(self.host_info)

    def _monitor_and_mitigate(self):
        self.logger.info("Monitor and Mitigate thread started")
        if os.path.exists('port_stats.csv'):
            os.remove('port_stats.csv')
        while True:
            # Sezione di monitoraggio
            for dp in self.datapaths.values():
                self._request_stats(dp)
            self._stats_csv() 

            # Sezione di mitigazione
            for (dpid, port_no), block_time in list(self.blocked_ports.items()):
                if (time.time() - block_time) > 30:
                    self._unblock_port(dpid, port_no)
                    del self.blocked_ports[(dpid, port_no)]
            self.logger.info(f"\n____PORTE ATTUALMENTE BLOCCATE: {list(self.blocked_ports.keys())}____")
         
            time.sleep(2)

    def _block_port(self, dpid, port_no):
        self.monitoring_list.remove((dpid, port_no))

        datapath = self.datapaths[dpid]
        parser = datapath.ofproto_parser

        # Create a match for incoming traffic on the port
        match = parser.OFPMatch(in_port=port_no)

        # Create an action to drop packets
        actions = []

        self.add_flow(datapath, 2, match, actions)

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=0, in_port=port_no, actions=actions, data=None)
        datapath.send_msg(out)

        self.logger.info(f'\n\n*************PORTA {(dpid, port_no)} RIMOSSA DALLA monitoring_list E BLOCCATA*************')

    def _unblock_port(self, dpid, port_no):
        self.logger.info(f'\n\n*************PORTA {(dpid, port_no)} SBLOCCATA*************')

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
                if (dpid, port_no) in self.host_info.get(dpid, {}):
                    self.monitoring_list.append((dpid, port_no))
                    self.logger.info(f'\n*************PORTA {(dpid, port_no)} AGGIUNTA ALLA monitoring_list: {self.monitoring_list}*************')
                else:
                    self.logger.info(f'\nLA PORTA {(dpid, port_no)} È ATTRAVERSATA DA TRAFFICO INTERMEDIO -> NON AGGIUNTA ALLA monitoring_list')
            elif (dpid, port_no) in self.monitoring_list and (dpid, port_no) not in self.blocked_ports:
                self.logger.warning(f'\n*************LA PORTA {(dpid, port_no)} È GIÀ IN monitoring_list: {self.monitoring_list}*************')
        elif (dpid, port_no) in self.monitoring_list:
            self.logger.info(f'\n*************LA PORTA {(dpid, port_no)} È STATA RIMOSSA DALLA monitoring_list*************')
            self.monitoring_list.remove((dpid, port_no))

    def _update_host_info(self, dpid, port_no, src_mac):
        if dpid not in self.host_info:
            self.host_info[dpid] = {}
        self.host_info[dpid][src_mac] = port_no
        self.logger.info(f"Host added: MAC={src_mac}, Switch={dpid}, Port={port_no}")
        self._print_current_hosts()
    
    def _print_current_hosts(self):
        self.logger.info("Current connected hosts:")
        for dpid, hosts in self.host_info.items():
            for mac, port in hosts.items():
                self.logger.info(f"  - Switch: {dpid}, MAC: {mac}, Port: {port}")
    
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_change_handler(self, ev):
        port = ev.port
        dpid = ev.datapath.id
    
        # When a new host connects
        if ev.status == 'ADD':
            self.logger.info(f'Host connected: {port.name} on switch {dpid}, MAC={port.hw_addr}')
            self._update_host_info(dpid, port.port_no, port.hw_addr)
    
        # When a host disconnects
        elif ev.status == 'DELETE':
            self.logger.info(f'Host disconnected: {port.name} from switch {dpid}, MAC={port.hw_addr}')
            if dpid in self.host_info and port.hw_addr in self.host_info[dpid]:
                del self.host_info[dpid][port.hw_addr]
                self._print_current_hosts()  # Print the updated host list after disconnection
    
        # When a port status changes
        elif ev.status == 'MODIFY':
            self.logger.info(f'Port modified: {port.name} on switch {dpid}')


    def _stats_csv(self):
        with open('port_stats.csv', mode='a', newline='') as file:
            writer = csv.writer(file)
            if file.tell() == 0:  # If file is empty, write header
                writer.writerow(['dpid', 'port_no', 'rx_bytes', 'tx_bytes', 'rx_throughput', 'tx_throughput'])
            for dpid, ports in self.port_stats.items():
                for port_no, stats in ports.items():
                    writer.writerow([dpid, port_no, stats['rx_bytes'], stats['tx_bytes'],
                                     stats.get('rx_throughput', 0), stats.get('tx_throughput', 0)])
