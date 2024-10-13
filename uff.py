def _monitor_port(self, dpid, port_no):
        active_sp=[]
        rx_throughput = self.port_stats[dpid][port_no].get('rx_throughput', 0)
    
        self.active_ports = [p for p in self.port_stats[dpid] if self.port_stats[dpid][p].get('rx_throughput', 0) > self.lower_threshold]
        self.num_active_ports = len(self.active_ports) - len(self.blocked_ports)
        if self.port_stats[dpid][port_no].get('rx_throughput',0)>self.lower_threshold:
            active_sp.append[(dpid,port_no)]
        

        print(active_sp)
        for(dpid, port_no) in self.host_info:
            if (dpid, port_no) in active_sp:
                self.host_info_updated[(dpid,port_no)]=self.host_info[(dpid,port_no)]
        print(self.host_info_updated)
