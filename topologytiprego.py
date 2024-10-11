import json  # Aggiungi questa importazione

class Environment(object):
    def __init__(self):
        ...
        self.create_topology()  # Creiamo la topologia
        self.export_topology()   # Esportiamo la topologia in JSON

    def create_topology(self):
        # Codice esistente per creare la topologia
        self.h1 = self.net.addHost('h1', mac ='00:00:00:00:00:01', ip= '10.0.0.1')
        self.h2 = self.net.addHost('h2', mac ='00:00:00:00:00:02', ip= '10.0.0.2')
        self.h3 = self.net.addHost('h3', mac ='00:00:00:00:00:03', ip= '10.0.0.3')

        self.cpe1 = self.net.addSwitch('s1', cls=OVSKernelSwitch)
        self.cpe2 = self.net.addSwitch('s2', cls=OVSKernelSwitch)
        self.cpe3 = self.net.addSwitch('s3', cls=OVSKernelSwitch)
        self.cpe4 = self.net.addSwitch('s4', cls=OVSKernelSwitch)

        # Aggiungi i link
        self.net.addLink(self.h1, self.cpe1, bw=6, delay='0.0025ms')
        self.net.addLink(self.h2, self.cpe2, bw=6, delay='0.0025ms')  
        self.net.addLink(self.cpe1, self.cpe3, bw=3, delay='25ms')
        self.net.addLink(self.cpe2, self.cpe3, bw=3, delay='25ms')
        self.net.addLink(self.cpe3, self.cpe4, bw=3, delay='25ms')
        self.net.addLink(self.cpe4, self.h3, bw=6, delay='0.0025ms')

    def export_topology(self):
        topology_info = {
            "hosts": {
                "h1": {"mac": "00:00:00:00:00:01", "ip": "10.0.0.1"},
                "h2": {"mac": "00:00:00:00:00:02", "ip": "10.0.0.2"},
                "h3": {"mac": "00:00:00:00:00:03", "ip": "10.0.0.3"},
            },
            "switches": {
                "s1": ["h1"],
                "s2": ["h2"],
                "s3": ["s1", "s2", "s4"],
                "s4": ["s3", "h3"],
            }
        }

        with open('topology.json', 'w') as f:
            json.dump(topology_info, f)

...
if __name__ == '__main__':

    setLogLevel('info')
    info('starting the environment\n')
    env = Environment()

    info("*** Running CLI\n")
    CLI(env.net)
