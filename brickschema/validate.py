"""
The `validate` module implements a wrapper of [pySHACL](https://github.com/RDFLib/pySHACL) to
validate an ontology graph against default Brick Schema constraints (called *shapes*) and user-defined
shapes
"""
import sys
import argparse
import logging
from rdflib import Graph, Namespace, URIRef, BNode
from rdflib.plugins.sparql import prepareQuery
from .namespaces import BRICK, A, RDF, RDFS, BRICK, BSH, SH, SKOS, bind_prefixes
import pyshacl
import io
import pkgutil

class Validate():
    """
    Validates a data graph against Brick Schema and basic SHACL constraints for Brick.  Allows adding
    constraits specific to the user's ontology.
    """

    # build accumulative namespace index from participating files
    # build list of violations, each is a graph
    def __init__(self, attachOffender=True):
        self.log = logging.getLogger()
        self.log.setLevel(logging.DEBUG if hasattr(sys, '_called_from_test') else logging.WARNING)
        self.log.info('Validate init')

        self.attachOffender = attachOffender

        # Read in Brick.ttl.  Remove rdfs:domain and rdfs:range.  The modified
        # ontology will be used for pySHACL reasoning.  See DESIGN.md for more discussion.
        data = pkgutil.get_data(__name__, "ontologies/Brick.ttl").decode()
        self.brickG = Graph()
        self.brickG.parse(source=io.StringIO(data), format='turtle')

        self.namespaceDict = {}
        self.__buildNamespaceDict(self.brickG)
        self.brickG.update('DELETE { ?s rdfs:domain ?o .} WHERE { ?s rdfs:domain ?o . }',
                           initNs=self.namespaceDict)
        self.brickG.update('DELETE { ?s rdfs:range ?o .} WHERE { ?s rdfs:range ?o . }',
                           initNs=self.namespaceDict)

        # Read in basic shapes for Brick.
        data = pkgutil.get_data(__name__, "ontologies/BrickShape.ttl").decode()
        self.shapeG = Graph()
        self.shapeG.parse(source=io.StringIO(data), format='turtle')


    def validate(self, data_graph, shacl_graph=None, ont_graph=None,
                 inference='rdfs', abort_on_error=False, advanced=True,
                 meta_shacl=True, debug=False):
        """
        Validates data_graph against shacl_graph and ont_graph.

        Args:
            shacl_graph: default to BrickShape.ttl
            ont_graph: default to Brick.ttl

        Returns:
            (tuple) (conforms,  violation graph if not conforming, result text)
        """

        self.log.info('wrapper function for pySHACL validate()')

        sg = shacl_graph if shacl_graph else self.shapeG
        og = ont_graph if ont_graph else self.brickG

        self.data_graph = data_graph
        (self.conforms, self.results_graph, self.results_text) = pyshacl.validate(
            data_graph, shacl_graph=sg, ont_graph=og,
            inference=inference, abort_on_error=abort_on_error,
            meta_shacl=meta_shacl, debug=debug)

        if self.conforms or (not self.attachOffender):
            return (self.conforms, self.results_graph, self.results_text)

        # find and attach offending triples to each violation and pack the violations
        # as results_graph

        self.__attachOffendingTriples()
        violationsG = Graph()
        for (k, v) in self.violationDict.items():
            violationsG = violationsG + v
        return (self.conforms, violationsG, self.results_text)

    def addShapeFile(self, shapeFile):
        """
        Add additional SHACL shape file into the existing shape graph.
        """
        self.log.info('load shape file %s' % shapeFile)
        g = Graph()
        g.parse(shapeFile, format='turtle')
        print(len(self.shapeG), len(g))
        self.shapeG = self.shapeG + g
        print(len(self.shapeG))


    def accumulatedNamespaces(self):
        """
        Convenient function to return the accmulated namepace dictionary.
        """
        return self.namespaceDict

    def violationList(self):
        """
        Convenient function to return the violation graphs as a list.
        """
        return list(self.violationDict.values())


    # Post process after calling pySHACL.validate to find offending
    # triple(s) for each violation.
    def __attachOffendingTriples(self):
        self.log.info('find offending triple(s) for each violation')

        self.namespaceDict = {}
        self.__buildNamespaceDict(self.results_graph)
        self.__buildNamespaceDict(self.data_graph)

        # results_graph from pyshacl.validate() is a list of graphs.
        # Some graphs are violation graphs each representing
        # a violation with the sh:result predicate.
        # There are also other graphs without sh:result and we
        # don't care about them.
        # To filter out the unwanted graphs, we
        # iterate the results_graph twice:
        # Round 1: Create a graph for each violation and index it.
        # Round 2: Add all triples belonging to a violation to proper entry.

        self.violationDict = {}
        for (s, p, o) in self.results_graph:
            if (o not in self.violationDict) and (p == SH.result):
                self.violationDict[o] = Graph()
                for n in self.namespaceDict:
                    self.violationDict[o].bind(n, self.namespaceDict[n])

        for (s, p, o) in self.results_graph:
            if s in self.violationDict:
                self.violationDict[s].add((s, p, o))

        # find the offending triple(s) for each violation graph and add into it
        for k, violation in self.violationDict.items():
            self.__triplesForOneViolation(violation)

    # Load namespaces into a dictionary which is accumulative with
    # the shape graph and data graph.
    def __buildNamespaceDict(self, g):
        for (prefix, path) in g.namespaces():
            assert (prefix not in self.namespaceDict) or \
                (Namespace(path) == self.namespaceDict[prefix]), \
                "Same prefix \'%s\' used for %s and %s" % \
                (prefix, self.namespaceDict[prefix], path)

            if prefix not in self.namespaceDict:
                self.namespaceDict[prefix] = Namespace(path)

    # Query data graph and return the list of resulting triples
    def __queryDataGraph(self, s, p, o):
        q = prepareQuery('SELECT ?s ?p ?o WHERE {%s %s %s .}' %
                         (s if s else '?s',
                          p if p else '?p',
                          o if o else '?o'),
                         initNs=self.namespaceDict
                         )
        res = self.data_graph.query(q)
        assert len(res), \
            'Must have at lease one triple like \'%s %s %s\'' % (s, p, o)
        return res


    # Take one contraint violation (a graph) and a sh: predicate,
    # find the object which is a node in the data graph.
    # Return the object found or None.
    def __violationPredicateObj(self, violation, predicate, mustFind=True):
        q = prepareQuery('SELECT ?s ?p ?o WHERE {?s %s ?o .}' % predicate,
                         initNs=self.namespaceDict
                        )
        res = violation.query(q)
        if mustFind:
            assert len(res) == 1, 'Must have predicate \'%s\'' % predicate
        if len(res):
            for (s, p, o) in res:
                return o
        return None  # Ok to miss certain predicate, such as sh:resultPath


    # Take one contraint violation (a graph) and find the potential offending
    # triples.  Return the triples in a list.
    def __triplesForOneViolation(self, violation):
        resultPath = self.__violationPredicateObj(violation,
                                                  'sh:resultPath',
                                                  mustFind=False)
        if resultPath:
            focusNode = self.__violationPredicateObj(violation, 'sh:focusNode')
            valueNode = self.__violationPredicateObj(violation,
                                                     'sh:value',
                                                     mustFind=False)

            # TODO: Although we haven't seen a violation with sh:resultPath where
            # focusNode and valueNode are the same, the case should be considered.
            # The triple probably should be queried using queryDataGraph() instead
            # of assuming focusNode is the subject here.

            g = Graph()
            g.add((focusNode, resultPath, valueNode))
            violation.add((BNode(), BSH['offendingTriple'], g))
            return

        # Without sh:resultPath in the violation. We are currently only concerned
        # with the RDFS.domain shape.
        sourceShape = self.__violationPredicateObj(violation, 'sh:sourceShape')
        (bsh, shapeName) = sourceShape.split('#')

        if shapeName.endswith('DomainShape'):
            # For a brick property xyz with RDFS.domain predicate, the shape's name
            # is bsh:xyzDomainShape.  Here we tease out brick:xyz to make the query.
            brickProp = shapeName[:-len('DomainShape')]
            path = 'brick:' + brickProp
            fullPath = self.namespaceDict['brick'] + brickProp

            # The full name (http...) of the focusNode doesn't seem to work
            # in the query.  Therefore make a prefixed version for the query.
            focusNode = self.__violationPredicateObj(violation, 'sh:focusNode')
            (ns, name) = focusNode.split('#')
            namespaces = [key  for (key, value) in self.namespaceDict.items() \
                          if Namespace(ns+'#') == value]
            assert len(namespaces), "Must find a prefix for %s" % focusNode
            res = self.__queryDataGraph('%s:%s' % (namespaces[0], name), path, None)

            # Due to inherent ambiguity of this kind of shape,
            # multiple triples may be found.
            for (s, p, o) in res:
                g = Graph()
                g.add((focusNode, URIRef(fullPath), o))
                violation.add((BNode(), BSH['offendingTriple'], g))
            return

        # When control reaches here, a handler is missing for the violation.
        self.log.error('no triple finder for violation %s' % g.serialize(format='ttl'))

        return
    # end of triplesForOneViolation()

# end of class Validate()

class ResultsSerialize():
    """
    Serializes violations (with offending tripple(s) already attatched).
    """

    def __init__(self, violationList, namespaceDict, output):
        self.log = logging.getLogger()
        self.log.setLevel(logging.DEBUG if hasattr(sys, '_called_from_test') else logging.WARNING)

        self.violationList = violationList
        self.outFile = output
        self.namespaceDict = namespaceDict


    # Serialize and streamline (remove @prefix lines) a grpah and append to output
    def __appendGraph(self, msg, g):
        if msg:
            self.outFile.write(msg)
        for n in self.namespaceDict:
            g.bind(n, self.namespaceDict[n])

        for b_line in g.serialize(format='ttl').splitlines():
            line = b_line.decode('utf-8')
            # skip prefix, offendingTriple and blank line
            if (not line.startswith('@prefix')) and \
               ('offendingTriple' not in line) \
               and line.strip():
                self.outFile.write(line)
                self.outFile.write('\n')


    def __appendViolation(self, msg, g):
        # first print the violation body
        self.__appendGraph(msg, g)

        # tease out the triples with offendingTriple as predicate
        tripleGraphs = []
        for (s, p, o) in g:
            if p == BSH['offendingTriple']:
                tripleG = Graph()
                for (s1, p1, o1) in o:
                    tripleG.add((s1, p1, o1))
                tripleGraphs.append(tripleG)

        if len(tripleGraphs) == 0:
            self.outFile.write('Please add triple finder for the above violation!!!\n')
            return

        if len(tripleGraphs) == 1:
            self.outFile.write('Offending triple:\n')
        else:
            self.outFile.write('Potential offending triples:\n')
        for tripleG in tripleGraphs:
            self.__appendGraph(None, tripleG)


    def appendToOutput(self):
        self.outFile.write('\nAdditional info (%d constraint violations with offending triples):\n' %
                           len(self.violationList))

        # Print each violation graph, find and print the offending triple(s), too
        for g in self.violationList:
            self.__appendViolation('\nConstraint violation:\n', g)

# end of class ResultsSerialize()

# __main to avoid being included in the api documentation
def __main():
    parser = argparse.ArgumentParser(description='pySHACL wrapper for reporting constraint violating triples.')
    parser.add_argument('data', metavar='DataGraph', type=argparse.FileType('rb'),
                        help='Data graph file.')
    parser.add_argument('-s', '--shacl', dest='shacl', action='store', nargs='?',
                        help='SHACL shapes graph file (default to BrickShape.ttl).')
    parser.add_argument('-e', '--ont-graph', dest='ont', action='store', nargs='?',
                        help='Ontology graph file (default to Brick.ttl).')
    parser.add_argument('-i', '--inference', dest='inference', action='store',
                        default='rdfs', choices=('none', 'rdfs', 'owlrl', 'both'),
                        help='Type of inference against data graph before validating.')
    parser.add_argument('-m', '--metashacl', dest='metashacl', action='store_true',
                        default=False,
                        help='Validate SHACL shapes graph against shacl-shacl '
                        'shapes graph before validating data graph.')
    parser.add_argument('-a', '--advanced', dest='advanced', action='store_true',
                        default=False,
                        help='Enable features from SHACL Advanced Features specification.')
    parser.add_argument('--abort', dest='abort', action='store_true',
                        default=False, help='Abort on first error.')
    parser.add_argument('-d', '--debug', dest='debug', action='store_true',
                        default=False, help='Output additional runtime messages.')
    parser.add_argument('-o', '--output', dest='output', nargs='?',
                        type=argparse.FileType('w'),
                        help='Send output to a file (default to stdout).',
                        default=sys.stdout)

    args = parser.parse_args()

    dataG = Graph()
    dataG = dataG.parse(args.data, format='turtle')

    shaclG = None
    if args.shacl:
        shaclG = Graph()
        shaclG.parse(args.shacl, format='turtle')

    ontG = None
    if args.ont:
        ontG = Graph()
        ontG.parse(args.ont, format='turtle')

    vModule = Validate()
    (conforms, results_graph, results_text) = vModule.validate(
        dataG, shacl_graph=shaclG, ont_graph=ontG,
        inference=args.inference, abort_on_error=args.abort,
        advanced=args.advanced, meta_shacl=args.metashacl, debug=args.debug)
    args.output.write(results_text)

    if not conforms:
        ResultsSerialize(vModule.violationList(),
                         vModule.accumulatedNamespaces(),
                         args.output).appendToOutput()
    args.output.close()
    exit(0 if conforms else -1)

if __name__ == "__main__":
    __main()
