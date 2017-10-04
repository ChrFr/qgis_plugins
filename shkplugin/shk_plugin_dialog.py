# -*- coding: utf-8 -*-
"""
/***************************************************************************
 SHKPluginDialog
                                 A QGIS plugin
 Plugin zur Berechnung von Erreichbarkeiten im Saale-Holzland-Kreis

                             -------------------
        begin                : 2017-07-20
        git sha              : $Format:%H$
        copyright            : (C) 2017 by GGR
        email                : franke@ggr-planung.de
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

import os
from PyQt4 import QtGui, uic, QtCore
from PyQt4.QtXml import QDomDocument
from osgeo import gdal
from time import time
from qgis.core import (QgsDataSourceURI, QgsVectorLayer, 
                       QgsMapLayerRegistry, QgsRasterLayer,
                       QgsProject, QgsLayerTreeLayer, QgsRectangle,
                       QgsVectorFileWriter, QgsComposition)
from qgis.gui import QgsLayerTreeMapCanvasBridge
from qgis.utils import iface
import numpy as np
from collections import defaultdict
import pickle
from filter_tree import FilterTree

FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'shk_plugin_dialog_base.ui'))

from config import Config
from connection import DBConnection, Login
from queries import (get_values, update_erreichbarkeiten,
                     update_gemeinde_erreichbarkeiten)
from ui_elements import (LabeledRangeSlider, SimpleSymbology,
                         SimpleFillSymbology, 
                         GraduatedSymbology, WaitDialog,
                         EXCEL_FILTER, KML_FILTER, PDF_FILTER,
                         browse_file, browse_folder)

config = Config()

SCHEMA = 'einrichtungen'

basepath = os.path.split(__file__)[0]
FILTER_XML = os.path.join(os.path.split(__file__)[0], "filter.xml")
OSM_XML = os.path.join(basepath, 'osm_map.xml')
GOOGLE_XML = os.path.join(basepath, 'google_maps.xml')
REPORT_TEMPLATE_PATH = os.path.join(basepath, 'report_template.qpt')
PICKLE_EX = '{category}_filter_tree.pickle'


class SHKPluginDialog(QtGui.QMainWindow, FORM_CLASS):
    def __init__(self, parent=None):
        """Constructor."""
        super(SHKPluginDialog, self).__init__(parent)
        # Set up the user interface from Designer.
        # After setupUI you can access any designer object by doing
        # self.<objectname>, and you can use autoconnect slots - see
        # http://qt-project.org/doc/qt-4.8/designer-using-a-ui-file.html
        # #widgets-and-dialogs-with-auto-connect
        self.setupUi(self)
        self.load_config()
        self.save_button.clicked.connect(self.save_config)
        self.browse_cache_button.clicked.connect(self.browse_cache)
        self.connect_button.clicked.connect(self.connect)
        self.login = None

        self.err_color_ranges =  [
            (0, 5, 'unter 5 Minuten', QtGui.QColor(37, 52, 148)), 
            (5, 10, '10 bis 15 Minuten', QtGui.QColor(42, 111, 176)), 
            (10, 15, '15 bis 20 Minuten', QtGui.QColor(56, 160, 191)), 
            (15, 20, '20 bis 25 Minuten', QtGui.QColor(103, 196, 189)), 
            (20, 30, '25 bis 30 Minuten', QtGui.QColor(179, 225, 184)), 
            (30, 60, '30 bis 60 Minuten', QtGui.QColor(255, 212, 184)), 
            (60, 120, '60 bis 120 Minuten', QtGui.QColor(251, 154, 153)), 
            (120, 99999999, 'mehr als 120 Minuten', QtGui.QColor(227, 88, 88)), 
        ]
        
        self.err_tags = {
            'Bildungseinrichtungen': 'bildung',
            'Gesundheit': 'gesundheit',
            'Nahversorgung': 'nahversorgung'
        }
        
        self.colors = {
            'Bildungseinrichtungen': 'orange',
            'Gesundheit': 'red',
            'Nahversorgung': '#F781F3'
        }
        
        self.borders = {
            'Gemeinden': QtCore.Qt.DotLine,
            'Verwaltungsgemeinschaften': QtCore.Qt.DashLine, 
            'Kreise': QtCore.Qt.SolidLine
        }
        
        
        for button in ['filter_button', 'filter_button_2', 'filter_button_3']:
            getattr(self, button).clicked.connect(self.apply_filters)
        
        self.filter_selection_button.clicked.connect(self.filter_selection)
        
        def refresh_filter():
            if not self.login:
                return
            category = self.get_selected_tab()
            filter_tree = self.categories[category]
            self.wait_call(lambda: filter_tree.from_xml(FILTER_XML))
        for button in ['refresh_button', 'refresh_button_2', 'refresh_button_3']:
            getattr(self, button).clicked.connect(refresh_filter)
    
        self.calculate_car_button.clicked.connect(self.calculate_car)
        self.calculate_ov_button.clicked.connect(
            lambda: self.wait_call(self.add_ov_layers))
        
        self.export_excel_button.clicked.connect(
            lambda: self.export_filter_layer(ext='xlsx'))
        self.export_kml_button.clicked.connect(
            lambda: self.export_filter_layer(ext='kml'))
        self.export_pdf_button.clicked.connect(self.create_report)
        
        self.canvas = iface.mapCanvas()
        
        # disable first tabs at startup (till connection)
        self.main_tabs.setTabEnabled(0, False)
        self.main_tabs.setTabEnabled(1, False)
        
    def init_filters(self): 
        self.categories = {
            'Bildungseinrichtungen': FilterTree('Bildungseinrichtungen',
                                                'bildung_gesamt',
                                                self.db_conn,
                                                self.schools_tree),
            'Gesundheit': FilterTree('Gesundheit',
                                     'gesundheit_gesamt',
                                     self.db_conn,
                                     self.medicine_tree),
            'Nahversorgung': FilterTree('Nahversorgung',
                                        'nahversorgung_gesamt',
                                        self.db_conn,
                                        self.supply_tree)
        }
        region_node = FilterTree.region_node(self.db_conn)
        start = time()
        for category, filter_tree in self.categories.iteritems():
            ## loading from pickled tree is preferred
            #fn = os.path.join(config.cache_folder, PICKLE_EX.format(
                #category=category))
            #if os.path.exists(fn):
                #with open(fn,'r') as f:
                    #tree = pickle.load(f)
                #setattr(self, self.categories[category][1], tree)
            #else:
            filter_tree.from_xml(FILTER_XML)  #, region_node=region_node)
        print('Filter init {}s'.format(time() - start))
    
    def load_config(self):
        db_config = config.db_config
        self.user_edit.setText(str(db_config['username']))
        self.pass_edit.setText(str(db_config['password']))
        self.db_edit.setText(str(db_config['db_name']))
        self.host_edit.setText(str(db_config['host']))
        self.port_edit.setText(str(db_config['port']))
        self.cache_edit.setText(str(config.cache_folder))
        
    def save_config(self):
        db_config = config.db_config
        db_config['username'] = str(self.user_edit.text())
        db_config['password'] = str(self.pass_edit.text())
        db_config['db_name'] = str(self.db_edit.text())
        db_config['host'] = str(self.host_edit.text())
        db_config['port'] = str(self.port_edit.text())
        config.cache_folder = str(self.cache_edit.text())

        config.write()

    def browse_cache(self):
        folder = browse_folder(self.cache_edit.text(),
                               u'Verzeichnis für den Cache wählen',
                               parent=self)
        if folder:
            self.cache_edit.setText(folder)

    def connect(self):
        
        db_config = config.db_config
        self.login = Login(host=db_config['host'], port=db_config['port'],
                           user=db_config['username'],
                           password=db_config['password'],
                           db=db_config['db_name'])
        self.db_conn = DBConnection(self.login)
        try:
            self.db_conn.fetch('SELECT * FROM pg_index')
        except:
            QtGui.QMessageBox.information(
                self, 'Fehler',
                (u'Verbindung zur Datenbank fehlgeschlagen.\n'
                u'Bitte überprüfen Sie die Einstellungen!'))
            self.login = None
            return
        #diag = WaitDialogThreaded(self.refresh, parent=self,
                          #parent_thread=iface.mainWindow())
        self.connection_label.setText('verbunden')
        self.connection_label.setStyleSheet('color: green')
        self.main_tabs.setTabEnabled(0, True)
        self.main_tabs.setTabEnabled(1, True)
        self.main_tabs.setCurrentIndex(0)
        self.wait_call(self.init_filters)
        self.wait_call(self.init_layers)

    def wait_call(self, function):
        '''
        display wait-dialog while executing function, not threaded
        (arcgis doesn't seem to handle multiple threads well)
        '''
        diag = WaitDialog(function, title='Bitte warten', parent=self)
        diag.show()
        function()
        diag.close()

    def add_db_layer(self, name, schema, tablename, geom,
                     symbology=None, uri=None, key=None, zoom=False,
                     group=None, where='', visible=True, to_shape=False):
        if not uri:
            uri = QgsDataSourceURI()
            uri.setConnection(self.login.host,
                              self.login.port,
                              self.login.db,
                              self.login.user,
                              self.login.password)
            uri.setDataSource(schema, tablename, geom, aKeyColumn=key,
                              aSql=where)
            uri = uri.uri(False)
        layer = QgsVectorLayer(uri, name, "postgres")
        remove_layer(name, group)
        if to_shape:
            path = config.cache_folder
            if not os.path.exists(path):
                os.mkdir(path)
            fn = os.path.join(path, name + '.shp')
            QgsVectorFileWriter.writeAsVectorFormat(
                layer, fn, 'utf-8', None, 'ESRI Shapefile')
            layer = QgsVectorLayer(fn, name, "ogr")
        # if no group is given, add to layer-root
        QgsMapLayerRegistry.instance().addMapLayer(layer, group is None)
        #if where:
            #layer.setSubsetString(where)
        if group:
            l = group.addLayer(layer)
        if symbology:
            symbology.apply(layer)
        if zoom:
            extent = layer.extent()
            self.canvas.setExtent(extent)
        iface.legendInterface().setLayerVisible(layer, visible)
        self.canvas.refresh()
        return layer
        
    def add_xml_background_map(self, xml, group=None, visible=True):
        layer_name = 'GoogleMaps'
        for child in group.children():
            pass
        layer = QgsRasterLayer(xml, layer_name)
    
        #layer = QgsRasterLayer("http://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer?f=json&pretty=true", "layer")
        remove_layer(layer_name, group)
        QgsMapLayerRegistry.instance().addMapLayer(layer, group is None)
        if group:
            group.addLayer(layer)
        iface.legendInterface().setLayerVisible(layer, visible)
    
    def add_wms_background_map(self, group=None):
        layer_name = 'OpenStreetMap WMS - by terrestris'
        remove_layer(layer_name, group)
        url = ('crs=EPSG:31468&dpiMode=7&format=image/png&layers=OSM-WMS&'
               'styles=&url=http://ows.terrestris.de/osm-gray/service')
        layer = QgsRasterLayer(url, layer_name, 'wms')
        QgsMapLayerRegistry.instance().addMapLayer(layer, group is None)
        if group:
            group.addLayer(layer)

    def init_layers(self):
        # just for the right initial order
        get_group('Filter')
        cat_group = get_group('Einrichtungen')
        border_group = get_group('Verwaltungsgrenzen')
        get_group('Erreichbarkeiten PKW')
        get_group(u'Erreichbarkeiten ÖPNV')
        self.add_wms_background_map(group=get_group('Hintergrundkarte'))
        self.add_xml_background_map(GOOGLE_XML,
                                    group=get_group('Hintergrundkarte'),
                                    visible=False)
        
        for name, tablename in [('Gemeinden', 'gemeinden_20161231'),
                                ('Verwaltungsgemeinschaften', 'vwg_20161231'),
                                ('Kreise', 'kreis_20161231')]:
            border_style = self.borders[name]
            symbology = SimpleFillSymbology(border_style=border_style)
            self.add_db_layer(name, 'verwaltungsgrenzen', tablename,
                              'geom', group=border_group, visible=False,
                              symbology=symbology)
        
        self.canvas.refresh()
    
        columns = ['spalte', 'editierbar', 'nur_auswahl_zulassen',
                   'auswahlmoeglichkeiten', 'alias']
        for category, filter_tree in self.categories.iteritems():
            table = filter_tree.tablename
            symbology = SimpleSymbology(self.colors[category])
            layer = self.add_db_layer(category, SCHEMA, table, 'geom_gk',
                                      symbology, group=cat_group, zoom=False)
            rows = get_values('editierbare_spalten', columns,
                              self.db_conn, schema='einrichtungen',
                              where="tabelle='{}'".format(table))
            editable_columns = [r.spalte for r in rows]
            if not rows:
                continue
            for i, f in enumerate(layer.fields()):
                try:
                    idx = editable_columns.index(f.name())
                    col, is_ed, is_sel, selections, alias = rows[idx]
                    if alias:
                        layer.addAttributeAlias(i, alias) 
                    if not is_ed:
                        layer.setEditorWidgetV2(i, 'Hidden')
                        continue
                    if is_sel and selections:
                        layer.setEditorWidgetV2(i, 'ValueMap')
                        layer.setEditorWidgetV2Config(i, dict(zip(selections, selections)))
                    elif is_sel:
                        layer.setEditorWidgetV2(i, 'UniqueValues')
                except:
                    layer.setEditorWidgetV2(i, 'Hidden')
        # zoom to extent
        self.canvas.refresh()
        extent = QgsRectangle()
        extent.setMinimal()
        for child in cat_group.children():
            if isinstance(child, QgsLayerTreeLayer):
                #print child.layer().extent()
                extent.combineExtentWith(child.layer().extent())
        self.canvas.setExtent(extent)
        self.canvas.refresh()
    
    def filter_selection(self):
        if not self.login:
            return
        # either name of layer or group have to match a category
        active_layer = iface.activeLayer()
        categories = self.categories.keys()
        layer_error = (u'Sie müssen im Layerfenster einen '
                       u'Layer auswählen, wahlweise aus den Gruppen '
                       u'Einrichtungen oder Filter.')
        if not active_layer:
            QtGui.QMessageBox.information(self, 'Fehler', layer_error)
            return
        else:
            layer_name = active_layer.name()
            if layer_name in categories:
                category = layer_name
            else:
                project_tree = QgsProject.instance().layerTreeRoot()
                layer_item = project_tree.findLayer(active_layer.id())
                group = layer_item.parent()
                group_name = group.name()
                if group_name in categories:
                    category = group_name
                else:
                    QtGui.QMessageBox.information(self, 'Fehler', layer_error)
                    return
            selected_feats = active_layer.selectedFeatures()
            if not selected_feats:
                msg = (u'Im ausgewählten Layer {} sind keine '
                       u'Einrichtungen selektiert.'.format(layer_name))
                QtGui.QMessageBox.information(self, 'Fehler', msg)
                return
    
            parent_group = get_group('Filter')
            subgroup = get_group(category, parent_group)
            ids = [str(f.attribute('id')) for f in selected_feats]
            name, ok = QtGui.QInputDialog.getText(
                self, 'Filter', 'Name des zu erstellenden Layers',
                text=get_unique_layer_name(category, subgroup))
            if not ok:
                return
            
            subset = 'id in ({})'.format(','.join(ids))
            layer = QgsVectorLayer(active_layer.source(), name, "postgres")
            remove_layer(name, subgroup)
            
            QgsMapLayerRegistry.instance().addMapLayer(layer, False)
            subgroup.addLayer(layer)
            layer.setSubsetString(subset)
            symbology = SimpleSymbology(self.colors[category], shape='triangle')
            symbology.apply(layer)
    
    def apply_filters(self):
        if not self.login:
            return
        category = self.get_selected_tab()
        parent_group = get_group('Filter')
        subgroup = get_group(category, parent_group)
        name, ok = QtGui.QInputDialog.getText(
            self, 'Filter', 'Name des zu erstellenden Layers',
            text=get_unique_layer_name(category, subgroup))
        if not ok:
            return
        
        filter_tree = self.categories[category]
        subset = filter_tree.to_sql_query()
        orig_layer = QgsMapLayerRegistry.instance().mapLayersByName(category)[0]
    
        remove_layer(name, subgroup)

        layer = QgsVectorLayer(orig_layer.source(), name, "postgres")
        QgsMapLayerRegistry.instance().addMapLayer(layer, False)
        subgroup.addLayer(layer)
        layer.setSubsetString(subset)
        symbology = SimpleSymbology(self.colors[category], shape='triangle')
        symbology.apply(layer)
    
    def get_filterlayer(self):

        items = []
        filter_group = get_group('Filter')
        for category in self.categories.iterkeys():
            subgroup = get_group(category, filter_group)
            subitems = [(category, c.layer().name())
                        for c in subgroup.children()]
            items += subitems
        if not items:
            QtGui.QMessageBox.information(
                self, 'Fehler', 'Es sind keine gefilterten Layer vorhanden.')
            return 
        item_texts = [u'{} - {}'.format(l, c) for l, c in items]
        sel, ok = QtGui.QInputDialog.getItem(self, 'Erreichbarkeiten',
                                              u'Gefilterten Layer auswählen',
                                              item_texts, 0, False)
        if not ok:
            return
        category, layer_name = items[item_texts.index(sel)]
        subgroup = get_group(category, filter_group)
        for child in subgroup.children():
            if child.layer().name() == layer_name:
                return category, child.layer()
        return
    
    def calculate_car(self):
        if not self.login:
            return
        res = self.get_filterlayer()
        if not res:
            return
        category, layer = res

        def run():
            query = layer.subsetString()
            
            tag = self.err_tags[category]
            results_group = get_group('Erreichbarkeiten PKW')
            layer_name = layer.name()
            subgroup = get_group(category, results_group)
            remove_layer(layer_name, subgroup)
            err_table = 'matview_err_' + tag
            gem_table = 'erreichbarkeiten_gemeinden_' + tag
            update_erreichbarkeiten(tag, self.db_conn, where=query)
            update_gemeinde_erreichbarkeiten(tag, self.db_conn)
            
            symbology = GraduatedSymbology('minuten', self.err_color_ranges,
                                           no_pen=True)
            self.add_db_layer(layer_name, 'erreichbarkeiten',
                              err_table, 'geom', key='grid_id',
                              symbology=symbology, group=subgroup,
                              zoom=False, to_shape=True)
            
            symbology = GraduatedSymbology('minuten_mittelwert'[:10],  # Shapefiles: max field-name length = 10
                                           self.err_color_ranges,
                                           no_pen=True)
            layer_name += ' Gemeindeebene'
            self.add_db_layer(layer_name, 'erreichbarkeiten',
                              gem_table, 'geom', key='ags',
                              symbology=symbology, group=subgroup, zoom=False,
                              to_shape=True)
            
        self.wait_call(run)

    def add_ov_layers(self):
        if not self.login:
            return
        results_group = get_group(u'Erreichbarkeiten ÖPNV')
        symbology = SimpleSymbology('yellow', shape='diamond')
        self.add_db_layer('Zentrale Orte', 'erreichbarkeiten',
                          'zentrale_orte', 'geom', key='id',
                          symbology=symbology, 
                          group=results_group)
        schema = 'erreichbarkeiten'
        mat_view = 'matview_err_ov'
        rows = self.db_conn.fetch(
            'SELECT DISTINCT(search_time) from {s}.{v}'.format(
            s=schema, v=mat_view))
        times = sorted([r.search_time for r in rows])
        symbology = GraduatedSymbology('minuten', self.err_color_ranges,
                                       no_pen=True)
        subgroup_to = get_group('Hinfahrt zu den zentralen Orten',
                                results_group)
        subgroup_from = get_group(u'Rückfahrt von den zentralen Orten',
                                       results_group)
        for time in times:
            layer_name = time.strftime("%H:%M")
            self.add_db_layer(layer_name, 'erreichbarkeiten',
                              'matview_err_ov', 'geom', key='id', 
                              symbology=symbology, group=subgroup_to,
                              where="search_time='{}'".format(time),
                              visible=True)
        subgroup_to.setIsMutuallyExclusive(
            True, initialChildIndex=max(len(times), 4))
        for child in subgroup_to.children():
            child.setExpanded(False)

    def get_selected_tab(self):
        idx = self.selection_tabs.currentIndex()
        tab_name = self.selection_tabs.tabText(idx)
        return tab_name
    
    def export_filter_layer(self, ext='xlsx'):
        res = self.get_filterlayer()
        if not res:
            return
        category, layer = res
        
        file_filter = EXCEL_FILTER if ext == 'xlsx' else KML_FILTER
        filepath = browse_file(None, 'Export', file_filter, save=True, 
                               parent=self)
        if not filepath:
            return
        driver = 'XLSX' if ext == 'xlsx'else 'KML'
        QgsVectorFileWriter.writeAsVectorFormat(
            layer, filepath, "utf-8", None, driver, False)

    def set_relations(self): 
        proj = QgsProject.instance()
        rel_manager = proj.relationManager()
        relations = rel_manager.relations()
        relation = QgsRelation()
        relations['Bildungseinrichtungen-{}'] = relation
    
    def create_report(self):
        filepath = browse_file(None, 'Export', PDF_FILTER, save=True, 
                               parent=self)
        if not filepath:
            return
        bridge = QgsLayerTreeMapCanvasBridge(
            QgsProject.instance().layerTreeRoot(), self.canvas)
        bridge.setCanvasLayers()
    
        template_file = file(REPORT_TEMPLATE_PATH)
        template_content = template_file.read()
        template_file.close()
        document = QDomDocument()
        document.setContent(template_content)
        composition = QgsComposition(self.canvas.mapSettings())
        # You can use this to replace any string like this [key]
        # in the template with a new value. e.g. to replace
        # [date] pass a map like this {'date': '1 Jan 2012'}
        #substitution_map = {
            #'DATE_TIME_START': 'foo',
            #'DATE_TIME_END': 'bar'}
        composition.loadFromTemplate(document)  #, substitution_map)
        # You must set the id in the template
        map_item = composition.getComposerItemById('map')
        map_item.setMapCanvas(self.canvas)
        map_item.zoomToExtent(self.canvas.extent())
        # You must set the id in the template
        legend_item = composition.getComposerItemById('legend')
        legend_item.updateLegend()
        composition.refreshItems()
        composition.exportAsPDF(filepath)

def get_group(groupname, parent_group=None):
    if not parent_group:
        parent_group = QgsProject.instance().layerTreeRoot()
    group = parent_group.findGroup(groupname)
    if not group:
        group = parent_group.addGroup(groupname)
    return group

def remove_group_layers(group):
    for child in group.children():
        if not hasattr(child, 'layer'):
            continue
        l = child.layer()
        QgsMapLayerRegistry.instance().removeMapLayer(l.id())

def remove_layer(name, group=None):
    
    if not group:
        ex = QgsMapLayerRegistry.instance().mapLayersByName(name)
        if len(ex) > 0:
            for e in ex:
                QgsMapLayerRegistry.instance().removeMapLayer(e.id())
    else:
        for child in group.children():
            if not hasattr(child, 'layer'):
                continue
            l = child.layer()
            if l and l.name() == name:
                QgsMapLayerRegistry.instance().removeMapLayer(l.id())
        
def get_unique_layer_name(name, group):
    orig_name = name
    retry = True
    i = 2
    while retry:
        retry = False
        for child in group.children():
            if not hasattr(child, 'layer'):
                continue
            l = child.layer()
            if l and l.name() == name:
                name = orig_name + '_{}'.format(i)
                retry = True
                i += 1
                break
    return name
        
if __name__ == '__main__':
    print
    
        
        
    