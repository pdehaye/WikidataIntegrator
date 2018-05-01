import copy
from collections import defaultdict
from functools import lru_cache
from itertools import chain

example_Q14911732 = {'P1057':
                         {'Q14911732-23F268EB-2848-4A82-A248-CF4DF6B256BC':
                              {'v': 'Q847102',
                               'ref': {'9d96507726508344ef1b8f59092fb350171b3d99':
                                           {('P248', 'Q29458763'), ('P594', 'ENSG00000123374')}},
                               'qual': {('P659', 'Q21067546'), ('P659', 'Q20966585')},
                               }
                          }
                     }


class FastRunContainer(object):
    def __init__(self, base_data_type, engine, sparql_endpoint_url=None,
                 base_filter=None, use_refs=False, ref_handler=None):
        self.prop_data = {}
        self.loaded_langs = {}
        self.statements = []
        self.base_filter = {}
        self.base_filter_string = ''
        self.prop_dt_map = {}
        self.current_qid = ''
        self.rev_lookup = defaultdict(set)
        self.base_data_type = base_data_type
        self.engine = engine
        self.sparql_endpoint_url = sparql_endpoint_url if sparql_endpoint_url else \
            getattr(engine,'sparql_endpoint_url', None)
        self.debug = False
        self.reconstructed_statements = []
        self.use_refs = use_refs
        self.ref_handler = ref_handler

        if base_filter and any(base_filter):
            self.base_filter = base_filter

            for k, v in self.base_filter.items():
                if v:
                    self.base_filter_string += '?item wdt:{0} wd:{1} . \n'.format(k, v)
                else:
                    self.base_filter_string += '?item wdt:{0} ?zz . \n'.format(k)

    def reconstruct_statements(self, qid):
        reconstructed_statements = []
        if qid not in self.prop_data:
            self.reconstructed_statements = reconstructed_statements
            return reconstructed_statements
        for prop_nr, dt in self.prop_data[qid].items():
            # get datatypes for qualifier props
            q_props = set(chain(*[[x[0] for x in d['qual']] for d in dt.values()]))
            r_props = set(chain(*[set(chain(*[[y[0] for y in x] for x in d['ref'].values()])) for d in dt.values()]))
            props = q_props | r_props
            for prop in props:
                if prop not in self.prop_dt_map:
                    self.prop_dt_map.update({prop: FastRunContainer.get_prop_datatype(prop_nr=prop,
                                                                                      engine=self.engine)})
            # reconstruct statements from frc (including qualifiers, and refs)
            for uid, d in dt.items():
                qualifiers = []
                for q in d['qual']:
                    f = [x for x in self.base_data_type.__subclasses__() if x.DTYPE ==
                         self.prop_dt_map[q[0]]][0]
                    qualifiers.append(f(q[1], prop_nr=q[0], is_qualifier=True))

                references = []
                for ref_id, refs in d['ref'].items():
                    this_ref = []
                    for ref in refs:
                        f = [x for x in self.base_data_type.__subclasses__() if x.DTYPE ==
                             self.prop_dt_map[ref[0]]][0]
                        this_ref.append(f(ref[1], prop_nr=ref[0], is_reference=True))
                    references.append(this_ref)

                f = [x for x in self.base_data_type.__subclasses__() if x.DTYPE ==
                     self.prop_dt_map[prop_nr]][0]
                reconstructed_statements.append(f(d['v'], prop_nr=prop_nr,
                                                  qualifiers=qualifiers, references=references))

        # this isn't used. done for debugging purposes
        self.reconstructed_statements = reconstructed_statements
        return reconstructed_statements

    def write_required(self, data, append_props=None, cqid=None):
        del_props = set()
        data_props = set()
        if not append_props:
            append_props = []

        for x in data:
            if x.value and x.data_type:
                data_props.add(x.get_prop_nr())
        write_required = False
        match_sets = []
        for date in data:
            # skip to next if statement has no value or no data type defined, e.g. for deletion objects
            current_value = date.get_value()
            if not current_value and not date.data_type:
                del_props.add(date.get_prop_nr())
                continue

            prop_nr = date.get_prop_nr()

            if prop_nr not in self.prop_dt_map:
                print("{} not found in fastrun".format(prop_nr))
                self.prop_dt_map.update({prop_nr: FastRunContainer.get_prop_datatype(prop_nr=prop_nr,
                                                                                     engine=self.engine)})
                self._query_data(prop_nr)

            # more sophisticated data types like dates and globe coordinates need special treatment here
            if self.prop_dt_map[prop_nr] == 'time':
                current_value = current_value[0]
            elif self.prop_dt_map[prop_nr] == 'wikibase-item':
                current_value = 'Q{}'.format(current_value)
            elif self.prop_dt_map[prop_nr] == 'globe-coordinate':
                write_required = True  # temporary workaround for handling globe coordinates

            if self.debug:
                print(current_value)

            if current_value in self.rev_lookup:
                # quick check for if the value has ever been seen before, if not, write required
                temp_set = set(self.rev_lookup[current_value])
            else:
                if self.debug:
                    print(current_value)
                    print('no matches for rev lookup')
                return True
            match_sets.append(temp_set)

        if cqid:
            matching_qids = {cqid}
        else:
            matching_qids = match_sets[0].intersection(*match_sets[1:])

        # check if there are any items that have all of these values
        # if not, a write is required no matter what
        if not len(matching_qids) == 1:
            if self.debug:
                print('no matches')
            return True

        qid = matching_qids.pop()
        self.current_qid = qid

        reconstructed_statements = self.reconstruct_statements(qid)
        tmp_rs = copy.deepcopy(reconstructed_statements)

        # handle append properties
        for p in append_props:
            app_data = [x for x in data if x.get_prop_nr() == p]  # new statements
            rec_app_data = [x for x in tmp_rs if x.get_prop_nr() == p]  # orig statements
            comp = []
            for x in app_data:
                for y in rec_app_data:
                    if x.get_value() == y.get_value():
                        if self.use_refs and self.ref_handler:
                            to_be = copy.deepcopy(y)
                            self.ref_handler(to_be, x)
                        else:
                            to_be = x
                        if y.equals(to_be, include_ref=self.use_refs):
                            comp.append(True)

            # comp = [True for x in app_data for y in rec_app_data if x.equals(y, include_ref=self.use_refs)]
            if len(comp) != len(app_data):
                if self.debug:
                    print("failed append: {}".format(p))
                return True

        tmp_rs = [x for x in tmp_rs if x.get_prop_nr() not in append_props and x.get_prop_nr() in data_props]
        # print("154: {}".format(tmp_rs))

        for date in data:
            # ensure that statements meant for deletion get handled properly
            reconst_props = set([x.get_prop_nr() for x in tmp_rs])
            if (not date.value or not date.data_type) and date.get_prop_nr() in reconst_props:
                if self.debug:
                    print('returned from delete prop handling')
                return True
            elif not date.value or not date.data_type:
                # Ignore the deletion statements which are not in the reconstructed statements.
                continue

            if date.get_prop_nr() in append_props:
                continue

            # this is where the magic happens
            # date is a new statement, proposed to be written
            # tmp_rs are the reconstructed statements == current state of the item
            bool_vec = []
            for x in tmp_rs:
                if x.get_value() == date.get_value() and x.get_prop_nr() not in del_props:
                    if self.use_refs and self.ref_handler:
                        to_be = copy.deepcopy(x)
                        self.ref_handler(to_be, date)
                    else:
                        to_be = date
                    if x.equals(to_be, include_ref=self.use_refs):
                        bool_vec.append(True)
                    else:
                        bool_vec.append(False)
                else:
                    bool_vec.append(False)
            """
            bool_vec = [x.equals(date, include_ref=self.use_refs, fref=self.ref_comparison_f) and
            x.get_prop_nr() not in del_props for x in tmp_rs]
            """

            if self.debug:
                print("bool_vec: {}".format(bool_vec))
                print('-----------------------------------')
                for x in tmp_rs:

                    if date == x and x.get_prop_nr() not in del_props:
                        print(x.get_prop_nr(), x.get_value(), [z.get_value() for z in x.get_qualifiers()])
                        print(date.get_prop_nr(), date.get_value(), [z.get_value() for z in date.get_qualifiers()])
                    else:
                        if x.get_prop_nr() == date.get_prop_nr():
                            print(x.get_prop_nr(), x.get_value(), [z.get_value() for z in x.get_qualifiers()])
                            print(date.get_prop_nr(), date.get_value(), [z.get_value() for z in date.get_qualifiers()])

            if not any(bool_vec):
                if self.debug:
                    print(len(bool_vec))
                    print('fast run failed at ', date.get_prop_nr())
                write_required = True
            else:
                tmp_rs.pop(bool_vec.index(True))

        if len(tmp_rs) > 0:
            if self.debug:
                print('failed because not zero')
                for x in tmp_rs:
                    print('xxx', x.get_prop_nr(), x.get_value(), [z.get_value() for z in x.get_qualifiers()])
                print('failed because not zero--END')
            write_required = True
        return write_required

    def init_language_data(self, lang, lang_data_type):
        """
        Initialize language data store
        :param lang: language code
        :param lang_data_type: 'label', 'description' or 'aliases'
        :return: None
        """
        if lang not in self.loaded_langs:
            self.loaded_langs[lang] = {}

        if lang_data_type not in self.loaded_langs[lang]:
            result = self._query_lang(lang=lang, lang_data_type=lang_data_type)
            data = self._process_lang(result)
            self.loaded_langs[lang].update({lang_data_type: data})

    def get_language_data(self, qid, lang, lang_data_type):
        """
        get language data for specified qid
        :param qid:
        :param lang: language code
        :param lang_data_type: 'label', 'description' or 'aliases'
        :return: list of strings
        If nothing is found:
            If lang_data_type == label: returns ['']
            If lang_data_type == description: returns ['']
            If lang_data_type == aliases: returns []
        """
        self.init_language_data(lang, lang_data_type)

        current_lang_data = self.loaded_langs[lang][lang_data_type]
        all_lang_strings = current_lang_data.get(qid, [])
        if not all_lang_strings and lang_data_type in {'label', 'description'}:
            all_lang_strings = ['']
        return all_lang_strings

    def check_language_data(self, qid, lang_data, lang, lang_data_type):
        """
        Method to check if certain language data exists as a label, description or aliases
        :param lang_data: list of string values to check
        :type lang_data: list
        :param lang: language code
        :type lang: str
        :param lang_data_type: What kind of data is it? 'label', 'description' or 'aliases'?
        :return:
        """
        all_lang_strings = set(x.strip().lower() for x in self.get_language_data(qid, lang, lang_data_type))

        for s in lang_data:
            if s.strip().lower() not in all_lang_strings:
                print('fastrun failed at label: {}, string: {}'.format(lang_data_type, s))
                return True

        return False

    def get_all_data(self):
        return self.prop_data

    def format_query_results(self, r, prop_nr):
        # r is the results of the sparql query in _query_data
        # r is modified in place
        # prop_nr is needed to get the property datatype to determine how to format the value
        prop_dt = FastRunContainer.get_prop_datatype(prop_nr=prop_nr, engine=self.engine)
        for i in r:
            for value in {'item', 'sid', 'qval', 'pq', 'pr', 'ref'}:
                if value in i:
                    i[value] = i[value]['value'].split('/')[-1]

            if 'v' in i:
                if i['v']['type'] == 'uri' and prop_dt == 'wikibase-item':
                    i['v'] = i['v']['value'].split('/')[-1]
                else:
                    i['v'] = i['v']['value']

                # Note: no-value and some-value don't actually show up in the results here
                # see for example: select * where { wd:Q7207 p:P40 ?c . ?c ?d ?e }
                if type(i['v']) is not dict:
                    self.rev_lookup[i['v']].add(i['item'])

            # handle ref
            if 'rval' in i:
                if ('datatype' in i['rval'] and i['rval']['datatype'] == 'http://www.w3.org/2001/XMLSchema#dateTime' and
                        not (i['rval']['value'].startswith("+") or i['rval']['value'].startswith("-"))):
                    i['rval']['value'] = '+' + i['rval']['value']
                ref_prop_dt = FastRunContainer.get_prop_datatype(prop_nr=i['pr'], engine=self.engine)
                if i['rval']['type'] == 'uri' and ref_prop_dt == 'wikibase-item':
                    i['rval'] = i['rval']['value'].split('/')[-1]
                else:
                    i['rval'] = i['rval']['value']

    def update_frc_from_query(self, r, prop_nr):
        # r is the output of format_query_results
        # this updates the frc from the query (result of _query_data)
        for i in r:
            qid = i['item']
            if qid not in self.prop_data:
                self.prop_data[qid] = {prop_nr: dict()}
            if prop_nr not in self.prop_data[qid]:
                self.prop_data[qid].update({prop_nr: dict()})
            if i['sid'] not in self.prop_data[qid][prop_nr]:
                self.prop_data[qid][prop_nr].update({i['sid']: dict()})
            # update values for this statement (not including ref)
            d = {'v': i['v']}
            self.prop_data[qid][prop_nr][i['sid']].update(d)

            if 'qual' not in self.prop_data[qid][prop_nr][i['sid']]:
                self.prop_data[qid][prop_nr][i['sid']]['qual'] = set()
            if 'pq' in i and 'qval' in i:
                self.prop_data[qid][prop_nr][i['sid']]['qual'].add((i['pq'], i['qval']))

            if 'ref' not in self.prop_data[qid][prop_nr][i['sid']]:
                self.prop_data[qid][prop_nr][i['sid']]['ref'] = dict()
            if 'ref' in i:
                if i['ref'] not in self.prop_data[qid][prop_nr][i['sid']]['ref']:
                    self.prop_data[qid][prop_nr][i['sid']]['ref'][i['ref']] = set()
                self.prop_data[qid][prop_nr][i['sid']]['ref'][i['ref']].add((i['pr'], i['rval']))

    def _query_data_refs(self, prop_nr):
        page_size = 10000
        page_count = 0
        num_pages = None
        if self.debug:
            # get the number of pages/queries so we can show a progress bar
            query = """SELECT (COUNT(?item) as ?c) where {{
                  {0}
                  ?item p:{1} ?sid .
            }}""".format(self.base_filter_string, prop_nr)
            r = self.engine.execute_sparql_query(query, endpoint=self.sparql_endpoint_url)['results']['bindings']
            count = int(r[0]['c']['value'])
            num_pages = (int(count) // page_size) + 1
            print("Query {}: {}/{}".format(prop_nr, page_count, num_pages))
        while True:
            query = """
                #Tool: wdi_core fastrun
                SELECT ?item ?qval ?pq ?sid ?v ?ref ?pr ?rval WHERE {
                  {
                    SELECT ?item ?v ?sid where {
                      **base_filter_string**
                      ?item p:**prop_nr** ?sid .
                      ?sid ps:**prop_nr** ?v .
                    } GROUP BY ?item ?v ?sid
                    ORDER BY ?sid
                    OFFSET **offset**
                    LIMIT **page_size**
                  }
                  OPTIONAL {
                    ?sid ?pq ?qval .
                    [] wikibase:qualifier ?pq
                  }
                  OPTIONAL {
                    ?sid prov:wasDerivedFrom ?ref .
                    ?ref ?pr ?rval .
                    [] wikibase:reference ?pr
                  }
                }""".replace("**offset**", str(page_count * page_size)). \
                replace("**base_filter_string**", self.base_filter_string). \
                replace("**prop_nr**", prop_nr).replace("**page_size**", str(page_size))

            results = self.engine.execute_sparql_query(query, endpoint=self.sparql_endpoint_url)['results']['bindings']
            self.format_query_results(results, prop_nr)
            self.update_frc_from_query(results, prop_nr)
            page_count += 1
            if num_pages:
                print("Query {}: {}/{}".format(prop_nr, page_count, num_pages))
            if len(results) == 0:
                break

    def _query_data(self, prop_nr):
        if self.use_refs:
            self._query_data_refs(prop_nr)
        else:
            query = '''
                #Tool: wdi_core fastrun
                select ?item ?qval ?pq ?sid ?v where {{
                  {0}

                  ?item p:{1} ?sid .

                  ?sid ps:{1} ?v .
                  OPTIONAL {{
                    ?sid ?pq ?qval .
                    [] wikibase:qualifier ?pq
                  }}
                }}
                '''.format(self.base_filter_string, prop_nr)
            r = self.engine.execute_sparql_query(query=query, endpoint=self.sparql_endpoint_url)['results']['bindings']
            self.format_query_results(r, prop_nr)
            self.update_frc_from_query(r, prop_nr)

    def _query_lang(self, lang, lang_data_type):
        """

        :param lang:
        :param lang_data_type:
        :return:
        """

        lang_data_type_dict = {
            'label': 'rdfs:label',
            'description': 'schema:description',
            'aliases': 'skos:altLabel'
        }

        query = '''
        #Tool: wdi_core fastrun
        SELECT ?item ?label WHERE {{
            {0}

            OPTIONAL {{
                ?item {1} ?label FILTER (lang(?label) = "{2}") .
            }}
        }}
        '''.format(self.base_filter_string, lang_data_type_dict[lang_data_type], lang)

        if self.debug:
            print(query)

        return self.engine.execute_sparql_query(query=query, endpoint=self.sparql_endpoint_url)['results']['bindings']

    @staticmethod
    def _process_lang(result):
        data = defaultdict(set)
        for r in result:
            qid = r['item']['value'].split("/")[-1]
            if 'label' in r:
                data[qid].add(r['label']['value'])
        return data

    @staticmethod
    @lru_cache(maxsize=10000)
    def get_prop_datatype(prop_nr, engine):
        item = engine(wd_item_id=prop_nr)
        return item.entity_metadata['datatype']

    def clear(self):
        """
        convinience function to empty this fastrun container
        """
        self.prop_dt_map = dict()
        self.prop_data = dict()
        self.rev_lookup = defaultdict(set)
