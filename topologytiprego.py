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
        # Creiamo un dizionario per memorizzare le informazioni della topologia
        topology_info = {}

        # Aggiungi informazioni sugli host con chiavi tuple
        for i, host in enumerate(self.net.hosts, start=1):
            topology_info[(i, 1)] = {  # Utilizziamo (i, 1) come chiave
                "mac": host.MAC(),
                "ip": host.IP()
            }

        # Se hai bisogno di collegare altri host o switch, aggiungi qui la logica per farlo
        for i, switch in enumerate(self.net.switches, start=1):
            # Supponiamo di voler rappresentare gli switch in un formato simile, puoi adattare come necessario
            topology_info[(i, 2)] = {
                "mac": switch.MAC(),
                "ip": None  # Gli switch non hanno un IP assegnato in questo contesto
            }

        # Scrivi le informazioni sulla topologia in un file JSON
        with open('topology.json', 'w') as f:
            json.dump(topology_info, f, indent=4)

...
if __name__ == '__main__':

    setLogLevel('info')
    info('starting the environment\n')
    env = Environment()

    info("*** Running CLI\n")
    CLI(env.net)
